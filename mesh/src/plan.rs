//! Plan deserialization — reads a `plan.skaal.lock` JSON blob.

use crate::error::MeshError;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// A backend specification from the solved plan.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackendSpec {
    /// Backend type identifier, e.g. `"redis"`, `"sqlite"`, `"dynamodb"`.
    pub backend_type: String,
    /// Backend-specific configuration (URL, region, table name, …).
    #[serde(default)]
    pub config: serde_json::Value,
}

/// A solved Skaal plan — the output of `skaal plan`.
///
/// Only the fields relevant to the mesh are decoded here; the rest are
/// ignored via `serde(deny_unknown_fields = false)` (the default).
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Plan {
    /// Application name.
    pub app_name: String,
    /// Solved backend assignments: storage variable name → backend spec.
    #[serde(default)]
    pub backends: HashMap<String, BackendSpec>,
    /// Registered function names.
    #[serde(default)]
    pub functions: Vec<String>,
    /// Registered agent type names.
    #[serde(default)]
    pub agents: Vec<String>,
}

impl Plan {
    /// Parse a plan from its JSON representation.
    ///
    /// Accepts an empty string or `"{}"` as a valid (empty) plan.
    pub fn from_json(json: &str) -> Result<Self, MeshError> {
        let trimmed = json.trim();
        if trimmed.is_empty() || trimmed == "{}" {
            return Ok(Plan::default());
        }
        serde_json::from_str(trimmed).map_err(MeshError::from)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_json_is_ok() {
        let p = Plan::from_json("").unwrap();
        assert_eq!(p.app_name, "");
        assert!(p.backends.is_empty());
    }

    #[test]
    fn parses_app_name() {
        let json = r#"{"app_name": "myapp", "backends": {}, "functions": [], "agents": []}"#;
        let p = Plan::from_json(json).unwrap();
        assert_eq!(p.app_name, "myapp");
    }

    #[test]
    fn parses_backends() {
        let json = r#"{
            "app_name": "myapp",
            "backends": {
                "counter.Counts": {"backend_type": "redis", "config": {"url": "redis://localhost"}}
            }
        }"#;
        let p = Plan::from_json(json).unwrap();
        assert_eq!(p.backends["counter.Counts"].backend_type, "redis");
    }
}
