//! Distributed state store abstraction.
//!
//! `StateStore` is a synchronous, thread-safe key/value store.  The
//! in-memory implementation is used for single-node deployments and tests;
//! future backends (Redis, etcd) will implement the same trait.

use crate::error::MeshError;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

// ── Trait ──────────────────────────────────────────────────────────────────────

/// Synchronous key/value store interface.
pub trait StateStore: Send + Sync {
    fn get(&self, key: &str) -> Result<Option<Value>, MeshError>;
    fn set(&self, key: &str, value: Value) -> Result<(), MeshError>;
    fn delete(&self, key: &str) -> Result<(), MeshError>;
    fn exists(&self, key: &str) -> Result<bool, MeshError>;
    /// Return all keys whose prefix matches `prefix`.  Empty prefix = all keys.
    fn keys_with_prefix(&self, prefix: &str) -> Result<Vec<String>, MeshError>;
    fn clear(&self) -> Result<(), MeshError>;
    fn len(&self) -> usize;
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

// ── InMemoryStateStore ─────────────────────────────────────────────────────────

/// Thread-safe in-memory state store backed by `Arc<RwLock<HashMap>>`.
///
/// Cloning the store gives a second handle to the *same* data.
#[derive(Clone, Default)]
pub struct InMemoryStateStore {
    data: Arc<RwLock<HashMap<String, Value>>>,
}

impl InMemoryStateStore {
    pub fn new() -> Self {
        Self::default()
    }
}

impl StateStore for InMemoryStateStore {
    fn get(&self, key: &str) -> Result<Option<Value>, MeshError> {
        let map = self
            .data
            .read()
            .map_err(|_| MeshError::StateError("state lock poisoned".into()))?;
        Ok(map.get(key).cloned())
    }

    fn set(&self, key: &str, value: Value) -> Result<(), MeshError> {
        let mut map = self
            .data
            .write()
            .map_err(|_| MeshError::StateError("state lock poisoned".into()))?;
        map.insert(key.to_owned(), value);
        Ok(())
    }

    fn delete(&self, key: &str) -> Result<(), MeshError> {
        let mut map = self
            .data
            .write()
            .map_err(|_| MeshError::StateError("state lock poisoned".into()))?;
        map.remove(key);
        Ok(())
    }

    fn exists(&self, key: &str) -> Result<bool, MeshError> {
        let map = self
            .data
            .read()
            .map_err(|_| MeshError::StateError("state lock poisoned".into()))?;
        Ok(map.contains_key(key))
    }

    fn keys_with_prefix(&self, prefix: &str) -> Result<Vec<String>, MeshError> {
        let map = self
            .data
            .read()
            .map_err(|_| MeshError::StateError("state lock poisoned".into()))?;
        let mut keys: Vec<String> = map
            .keys()
            .filter(|k| k.starts_with(prefix))
            .cloned()
            .collect();
        keys.sort();
        Ok(keys)
    }

    fn clear(&self) -> Result<(), MeshError> {
        let mut map = self
            .data
            .write()
            .map_err(|_| MeshError::StateError("state lock poisoned".into()))?;
        map.clear();
        Ok(())
    }

    fn len(&self) -> usize {
        self.data.read().map(|m| m.len()).unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn set_get_delete() {
        let store = InMemoryStateStore::new();
        store.set("foo", json!(42)).unwrap();
        assert_eq!(store.get("foo").unwrap(), Some(json!(42)));
        store.delete("foo").unwrap();
        assert_eq!(store.get("foo").unwrap(), None);
    }

    #[test]
    fn exists() {
        let store = InMemoryStateStore::new();
        assert!(!store.exists("x").unwrap());
        store.set("x", json!("hello")).unwrap();
        assert!(store.exists("x").unwrap());
    }

    #[test]
    fn keys_with_prefix() {
        let store = InMemoryStateStore::new();
        store.set("agent:1", json!(1)).unwrap();
        store.set("agent:2", json!(2)).unwrap();
        store.set("state:a", json!("a")).unwrap();

        let keys = store.keys_with_prefix("agent:").unwrap();
        assert_eq!(keys, vec!["agent:1", "agent:2"]);
    }

    #[test]
    fn clone_shares_data() {
        let store = InMemoryStateStore::new();
        let clone = store.clone();
        store.set("shared", json!(true)).unwrap();
        assert_eq!(clone.get("shared").unwrap(), Some(json!(true)));
    }
}
