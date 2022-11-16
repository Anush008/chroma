import os
import shutil
import time

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from chroma_server.worker import heavy_offline_analysis

from chroma_server.db.clickhouse import Clickhouse, get_col_pos
from chroma_server.index.hnswlib import Hnswlib
from chroma_server.types import AddEmbedding, QueryEmbedding, ProcessEmbedding, FetchEmbedding, CountEmbedding, RawSql, Results, SpaceKeyInput, DeleteEmbedding

from chroma_server.utils.telemetry.capture import Capture
from chroma_server.utils.error_reporting import init_error_reporting

from fastapi.middleware.cors import CORSMiddleware

chroma_telemetry = Capture()
chroma_telemetry.capture('server-start')
init_error_reporting()

from celery.result import AsyncResult

# Boot script
db = Clickhouse
ann_index = Hnswlib

app = FastAPI(debug=True)

# enable CORS
app.add_middleware(
    CORSMiddleware, allow_headers=["*"], allow_origins=["http://localhost:3000"], allow_methods=["*"]
)

# init db and index
app._db = db()
app._ann_index = ann_index()

# API Endpoints
@app.get("/api/v1")
async def root():
    '''Heartbeat endpoint'''
    return {"nanosecond heartbeat": int(1000 * time.time_ns())}
    

@app.post("/api/v1/add", status_code=status.HTTP_201_CREATED)
async def add(new_embedding: AddEmbedding):
    '''Save batched embeddings to database'''

    number_of_embeddings = len(new_embedding.embedding)

    if isinstance(new_embedding.model_space, str):
        model_space = [new_embedding.model_space] * number_of_embeddings
    elif len(new_embedding.model_space) == 1: 
        model_space = [new_embedding.model_space[0]] * number_of_embeddings
    else: 
        model_space = new_embedding.model_space
    
    if isinstance(new_embedding.dataset, str):
        dataset = [new_embedding.dataset] * number_of_embeddings
    elif len(new_embedding.dataset) == 1:
        dataset = [new_embedding.dataset[0]] * number_of_embeddings
    else: 
        dataset = new_embedding.dataset

    app._db.add(
        model_space, 
        new_embedding.embedding, 
        new_embedding.input_uri, 
        dataset,
        new_embedding.inference_class, 
        new_embedding.label_class
    )

    return {"response": "Added records to database"}

@app.post("/api/v1/fetch")
async def fetch(embedding: FetchEmbedding):
    '''
    Fetches embeddings from the database
    - enables filtering by where_filter, sorting by key, and limiting the number of results
    '''
    return app._db.fetch(embedding.where_filter, embedding.sort, embedding.limit, embedding.offset)

@app.post("/api/v1/delete")
async def delete(embedding: DeleteEmbedding):
    '''
    Deletes embeddings from the database
    - enables filtering by where_filter
    '''
    return app._db.delete(embedding.where_filter)

@app.get("/api/v1/count")
async def count(model_space: str = None):
    '''
    Returns the number of records in the database
    '''
    return {"count": app._db.count(model_space=model_space)}

@app.post("/api/v1/reset")
async def reset():
    '''
    Reset the database and index - WARNING: Destructive! 
    '''
    app._db = db()
    app._db.reset()
    app._ann_index = ann_index()
    app._ann_index.reset()
    return True

@app.post("/api/v1/get_nearest_neighbors")
async def get_nearest_neighbors(embedding: QueryEmbedding):
    '''
    return the distance, database ids, and embedding themselves for the input embedding
    '''
    if embedding.model_space is None:
        return {"error": "model_space is required"}

    ids = None
    filter_by_where = {}
    filter_by_where["model_space"] = embedding.model_space
    if embedding.inference_class is not None:
        filter_by_where["inference_class"] = embedding.inference_class
    if embedding.label_class is not None:
        filter_by_where["label_class"] = embedding.label_class
    if embedding.dataset is not None:
        filter_by_where["dataset"] = embedding.dataset

    if filter_by_where is not None:
        results = app._db.fetch(filter_by_where)
        ids = [str(item[get_col_pos('uuid')]) for item in results] 
    
    uuids, distances = app._ann_index.get_nearest_neighbors(embedding.model_space, embedding.embedding, embedding.n_results, ids)
    return {
        "ids": uuids,
        "embeddings": app._db.get_by_ids(uuids),
        "distances": distances.tolist()[0]
    }

@app.post("/api/v1/raw_sql")
async def raw_sql(raw_sql: RawSql):
    return app._db.raw_sql(raw_sql.raw_sql)

@app.post("/api/v1/create_index")
async def create_index(process_embedding: ProcessEmbedding):
    '''
    Currently generates an index for the embedding db
    '''
    fetch = app._db.fetch({"model_space": process_embedding.model_space}, columnar=True)
    chroma_telemetry.capture('created-index-run-process', {'n': len(fetch[2])})
    app._ann_index.run(process_embedding.model_space, fetch[1], fetch[2]) # more magic number, ugh

@app.post("/api/v1/process")
async def process(process_embedding: ProcessEmbedding):
    '''
    Currently generates an index for the embedding db
    '''
    fetch = app._db.fetch({"model_space": process_embedding.model_space}, columnar=True)
    chroma_telemetry.capture('created-index-run-process', {'n': len(fetch[2])})
    app._ann_index.run(process_embedding.model_space, fetch[1], fetch[2]) # more magic number, ugh

    task = heavy_offline_analysis.delay(process_embedding.model_space)
    chroma_telemetry.capture('heavy-offline-analysis')
    return JSONResponse({"task_id": task.id})

@app.post("/api/v1/tasks/{task_id}")
async def get_status(task_id):
    task_result = AsyncResult(task_id)
    result = {
        "task_id": task_id,
        "task_status": task_result.status,
        "task_result": task_result.result
    }
    return JSONResponse(result)

@app.post("/api/v1/get_results")
async def get_results(results: Results):

    # if there is no index, generate one
    if not app._ann_index.has_index(results.model_space):
        fetch = app._db.fetch({"model_space": results.model_space}, columnar=True)
        chroma_telemetry.capture('run-process', {'n': len(fetch[2])})
        print("Generating index for model space: ", results.model_space, " with ", len(fetch[2]), " embeddings")
        app._ann_index.run(results.model_space, fetch[1], fetch[2]) # more magic number, ugh
        print("Done generating index for model space: ", results.model_space)

    # if there are no results, generate them
    print("app._db.count_results(results.model_space): ", app._db.count_results(results.model_space))
    if app._db.count_results(results.model_space) == 0:
        print("starting heavy offline analysis")
        task = heavy_offline_analysis(results.model_space)
        print("ending heavy offline analysis")
        return app._db.return_results(results.model_space, results.n_results)

    else:
        return app._db.return_results(results.model_space, results.n_results)

@app.get("/api/v1/model_spaces")
async def get_model_spaces():
    return app._db.get_model_spaces()
    
@app.get("/api/v1/datasets")
async def get_datasets(model_space: str):
    return app._db.get_datasets(model_space)
