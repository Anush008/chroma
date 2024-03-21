use crate::errors::{ChromaError, ErrorCodes};
use thiserror::Error;

/// The distance function enum.
/// # Description
/// This enum defines the distance functions supported by indices in Chroma.
/// # Variants
/// - `Euclidean` - The Euclidean or l2 norm.
/// - `Cosine` - The cosine distance. Specifically, 1 - cosine.
/// - `InnerProduct` - The inner product. Specifically, 1 - inner product.
/// # Notes
/// See https://docs.trychroma.com/usage-guide#changing-the-distance-function
#[derive(Clone, Debug)]
pub(crate) enum DistanceFunction {
    Euclidean,
    Cosine,
    InnerProduct,
}

impl DistanceFunction {
    pub(crate) fn distance(&self, a: &[f32], b: &[f32]) -> f32 {
        match self {
            DistanceFunction::Euclidean => {
                // TODO: implement this in SSE/AVX SIMD
                // let mut sum = 0.0;
                // for i in 0..a.len() {
                //     sum += (a[i] - b[i]).powi(2);
                // }
                // sum.sqrt()
                // TODO: recheck the definition, do we just take ^2?
                a.iter().zip(b).map(|(x, y)| (x - y).abs()).sum()
            }
            DistanceFunction::Cosine => {
                todo!();
            }
            DistanceFunction::InnerProduct => {
                todo!();
            }
        }
    }
}

#[derive(Error, Debug)]
pub(crate) enum DistanceFunctionError {
    #[error("Invalid distance function `{0}`")]
    InvalidDistanceFunction(String),
}

impl ChromaError for DistanceFunctionError {
    fn code(&self) -> ErrorCodes {
        match self {
            DistanceFunctionError::InvalidDistanceFunction(_) => ErrorCodes::InvalidArgument,
        }
    }
}

impl TryFrom<&str> for DistanceFunction {
    type Error = DistanceFunctionError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "l2" => Ok(DistanceFunction::Euclidean),
            "cosine" => Ok(DistanceFunction::Cosine),
            "ip" => Ok(DistanceFunction::InnerProduct),
            _ => Err(DistanceFunctionError::InvalidDistanceFunction(
                value.to_string(),
            )),
        }
    }
}

impl Into<String> for DistanceFunction {
    fn into(self) -> String {
        match self {
            DistanceFunction::Euclidean => "l2".to_string(),
            DistanceFunction::Cosine => "cosine".to_string(),
            DistanceFunction::InnerProduct => "ip".to_string(),
        }
    }
}
