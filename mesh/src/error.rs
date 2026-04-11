//! Unified error type for the Skaal mesh.

use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyValueError};
use pyo3::PyErr;
use std::fmt;

/// All errors the mesh can produce.
#[derive(Debug, Clone)]
pub enum MeshError {
    /// An agent with the given (type, id) pair was not found.
    AgentNotFound { agent_type: String, agent_id: String },
    /// An agent with this ID is already registered.
    AgentAlreadyExists { agent_id: String },
    /// The plan JSON was malformed or missing required fields.
    InvalidPlan(String),
    /// A requested method was not found on an agent type.
    MethodNotFound { method: String, agent_type: String },
    /// A state-store operation failed (e.g. lock poisoned).
    StateError(String),
    /// A migration operation failed.
    MigrationError(String),
    /// JSON serialization/deserialization error.
    SerdeError(String),
    /// Channel operation failed.
    ChannelError(String),
}

impl fmt::Display for MeshError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AgentNotFound { agent_type, agent_id } => {
                write!(f, "Agent '{agent_type}/{agent_id}' not found in registry")
            }
            Self::AgentAlreadyExists { agent_id } => {
                write!(f, "Agent '{agent_id}' is already registered")
            }
            Self::InvalidPlan(msg) => write!(f, "Invalid plan: {msg}"),
            Self::MethodNotFound { method, agent_type } => {
                write!(f, "Method '{method}' not found on agent type '{agent_type}'")
            }
            Self::StateError(msg) => write!(f, "State error: {msg}"),
            Self::MigrationError(msg) => write!(f, "Migration error: {msg}"),
            Self::SerdeError(msg) => write!(f, "Serialization error: {msg}"),
            Self::ChannelError(msg) => write!(f, "Channel error: {msg}"),
        }
    }
}

// Convert MeshError to the most appropriate Python exception.
impl From<MeshError> for PyErr {
    fn from(err: MeshError) -> PyErr {
        match &err {
            MeshError::AgentNotFound { .. } | MeshError::MethodNotFound { .. } => {
                PyKeyError::new_err(err.to_string())
            }
            MeshError::InvalidPlan(_)
            | MeshError::MigrationError(_)
            | MeshError::SerdeError(_) => PyValueError::new_err(err.to_string()),
            _ => PyRuntimeError::new_err(err.to_string()),
        }
    }
}

impl From<serde_json::Error> for MeshError {
    fn from(err: serde_json::Error) -> Self {
        Self::SerdeError(err.to_string())
    }
}
