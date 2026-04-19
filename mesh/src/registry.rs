//! Agent registry — tracks all agent instances across the mesh.
//!
//! The registry is the single source of truth for which agents are live,
//! what type they are, and what state they're in.  It is safe to clone —
//! all clones share the same underlying data via `Arc`.

use crate::error::MeshError;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, RwLock};

// ── Status ─────────────────────────────────────────────────────────────────────

/// Lifecycle status of a single agent instance.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AgentStatus {
    Starting,
    Running,
    Idle,
    Stopping,
    Stopped,
    Error,
}

impl fmt::Display for AgentStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            AgentStatus::Starting => "starting",
            AgentStatus::Running => "running",
            AgentStatus::Idle => "idle",
            AgentStatus::Stopping => "stopping",
            AgentStatus::Stopped => "stopped",
            AgentStatus::Error => "error",
        };
        f.write_str(s)
    }
}

impl AgentStatus {
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "starting" => Some(AgentStatus::Starting),
            "running" => Some(AgentStatus::Running),
            "idle" => Some(AgentStatus::Idle),
            "stopping" => Some(AgentStatus::Stopping),
            "stopped" => Some(AgentStatus::Stopped),
            "error" => Some(AgentStatus::Error),
            _ => None,
        }
    }
}

// ── AgentEntry ─────────────────────────────────────────────────────────────────

/// A record for a single agent instance.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentEntry {
    pub agent_id: String,
    pub agent_type: String,
    pub status: AgentStatus,
    /// Instance index for multi-replica deployments (0-based).
    pub instance: u32,
    pub registered_at: String,
    pub last_active: String,
    /// Arbitrary caller-supplied metadata (serialised as JSON).
    pub metadata: serde_json::Value,
}

impl AgentEntry {
    pub fn new(agent_id: &str, agent_type: &str, instance: u32) -> Self {
        let now = Utc::now().to_rfc3339();
        Self {
            agent_id: agent_id.to_owned(),
            agent_type: agent_type.to_owned(),
            status: AgentStatus::Starting,
            instance,
            registered_at: now.clone(),
            last_active: now,
            metadata: serde_json::Value::Object(Default::default()),
        }
    }
}

// ── AgentRegistry ──────────────────────────────────────────────────────────────

/// Thread-safe, clone-friendly agent registry.
///
/// All clones share the same underlying `HashMap` via `Arc<RwLock<_>>`.
#[derive(Clone, Default)]
pub struct AgentRegistry {
    agents: Arc<RwLock<HashMap<String, AgentEntry>>>,
}

impl AgentRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    // ── Write operations ─────────────────────────────────────────────────────

    /// Register a new agent.  Returns `AgentAlreadyExists` if the ID is taken.
    pub fn register(
        &self,
        agent_type: &str,
        agent_id: &str,
        instance: u32,
        metadata: Option<serde_json::Value>,
    ) -> Result<AgentEntry, MeshError> {
        let mut map = self
            .agents
            .write()
            .map_err(|_| MeshError::StateError("registry lock poisoned".into()))?;

        if map.contains_key(agent_id) {
            return Err(MeshError::AgentAlreadyExists {
                agent_id: agent_id.to_owned(),
            });
        }

        let mut entry = AgentEntry::new(agent_id, agent_type, instance);
        if let Some(meta) = metadata {
            entry.metadata = meta;
        }
        map.insert(agent_id.to_owned(), entry.clone());
        Ok(entry)
    }

    /// Update an agent's status and refresh `last_active`.
    pub fn update_status(&self, agent_id: &str, status: AgentStatus) -> Result<(), MeshError> {
        let mut map = self
            .agents
            .write()
            .map_err(|_| MeshError::StateError("registry lock poisoned".into()))?;

        let entry = map.get_mut(agent_id).ok_or_else(|| MeshError::AgentNotFound {
            agent_type: String::new(),
            agent_id: agent_id.to_owned(),
        })?;
        entry.status = status;
        entry.last_active = Utc::now().to_rfc3339();
        Ok(())
    }

    /// Remove an agent from the registry.  No-op if the ID is unknown.
    pub fn deregister(&self, agent_id: &str) -> Result<(), MeshError> {
        let mut map = self
            .agents
            .write()
            .map_err(|_| MeshError::StateError("registry lock poisoned".into()))?;
        map.remove(agent_id);
        Ok(())
    }

    // ── Read operations ──────────────────────────────────────────────────────

    /// Look up an agent by ID.
    pub fn get(&self, agent_id: &str) -> Result<Option<AgentEntry>, MeshError> {
        let map = self
            .agents
            .read()
            .map_err(|_| MeshError::StateError("registry lock poisoned".into()))?;
        Ok(map.get(agent_id).cloned())
    }

    /// List agents, optionally filtered by type and/or status string.
    pub fn list(
        &self,
        agent_type_filter: Option<&str>,
        status_filter: Option<&str>,
    ) -> Result<Vec<AgentEntry>, MeshError> {
        let map = self
            .agents
            .read()
            .map_err(|_| MeshError::StateError("registry lock poisoned".into()))?;
        let entries = map
            .values()
            .filter(|e| {
                agent_type_filter.map_or(true, |t| e.agent_type == t)
                    && status_filter.map_or(true, |s| e.status.to_string() == s)
            })
            .cloned()
            .collect();
        Ok(entries)
    }

    /// Total number of registered agents.
    pub fn count(&self) -> usize {
        self.agents.read().map(|m| m.len()).unwrap_or(0)
    }

    /// Count agents matching an optional status filter.
    pub fn count_by_status(&self, status: &str) -> usize {
        self.agents
            .read()
            .map(|m| {
                m.values()
                    .filter(|e| e.status.to_string() == status)
                    .count()
            })
            .unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn register_and_get() {
        let reg = AgentRegistry::new();
        let entry = reg.register("Customer", "cust-1", 0, None).unwrap();
        assert_eq!(entry.agent_type, "Customer");
        assert_eq!(entry.status, AgentStatus::Starting);

        let got = reg.get("cust-1").unwrap().unwrap();
        assert_eq!(got.agent_id, "cust-1");
    }

    #[test]
    fn duplicate_registration_fails() {
        let reg = AgentRegistry::new();
        reg.register("Customer", "cust-1", 0, None).unwrap();
        let err = reg.register("Customer", "cust-1", 0, None).unwrap_err();
        assert!(matches!(err, MeshError::AgentAlreadyExists { .. }));
    }

    #[test]
    fn update_and_deregister() {
        let reg = AgentRegistry::new();
        reg.register("Customer", "cust-1", 0, None).unwrap();
        reg.update_status("cust-1", AgentStatus::Running).unwrap();
        assert_eq!(
            reg.get("cust-1").unwrap().unwrap().status,
            AgentStatus::Running
        );

        reg.deregister("cust-1").unwrap();
        assert!(reg.get("cust-1").unwrap().is_none());
    }

    #[test]
    fn list_with_filter() {
        let reg = AgentRegistry::new();
        reg.register("Customer", "c-1", 0, None).unwrap();
        reg.register("Customer", "c-2", 0, None).unwrap();
        reg.register("Order", "o-1", 0, None).unwrap();

        let customers = reg.list(Some("Customer"), None).unwrap();
        assert_eq!(customers.len(), 2);

        let orders = reg.list(Some("Order"), None).unwrap();
        assert_eq!(orders.len(), 1);
    }
}
