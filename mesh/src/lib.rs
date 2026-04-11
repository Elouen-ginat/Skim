//! skaal-mesh — Skaal runtime mesh exposed to Python via PyO3.
//!
//! The mesh is the control plane for a Skaal distributed deployment.  It
//! manages:
//!
//! * **Agent registry** — tracks every live agent instance across the cluster.
//! * **State store** — a shared key/value store for agent-local and shared state.
//! * **Migration controller** — 6-stage zero-downtime backend migration protocol.
//! * **Pub/sub channels** — in-process broadcast channels for inter-function messaging.
//!
//! All of the above are thread-safe and share data via `Arc`.  The Python
//! extension wraps them in a single `SkaalMesh` class.

use pyo3::prelude::*;
use serde_json::Value;

mod channel;
mod error;
mod migration;
mod plan;
mod registry;
mod state;

use channel::MeshChannel;
use error::MeshError;
use migration::MigrationController;
use plan::Plan;
use registry::{AgentRegistry, AgentStatus};
use state::{InMemoryStateStore, StateStore};

// ── SkaalMesh ──────────────────────────────────────────────────────────────────

/// The runtime mesh for a Skaal application.
///
/// Create one per application process (Python):
///
/// ```python
/// mesh = SkaalMesh("myapp", plan_json)
/// ```
///
/// All methods are synchronous and GIL-aware — they release the GIL during
/// any blocking work.
#[pyclass]
pub struct SkaalMesh {
    plan: Plan,
    registry: AgentRegistry,
    state: InMemoryStateStore,
    migration: MigrationController,
    channel: MeshChannel,
}

#[pymethods]
impl SkaalMesh {
    // ── Constructor ───────────────────────────────────────────────────────────

    /// Create a new `SkaalMesh` from an app name and an optional plan JSON string.
    ///
    /// Args:
    ///     app_name:  Application name (used as a namespace).
    ///     plan_json: JSON string produced by ``skaal plan``.  Pass ``""`` or
    ///                ``"{}"`` to start with an empty plan.
    #[new]
    #[pyo3(signature = (app_name, plan_json = ""))]
    pub fn new(app_name: String, plan_json: &str) -> PyResult<Self> {
        let plan = if plan_json.is_empty() {
            let mut p = Plan::default();
            p.app_name = app_name.clone();
            p
        } else {
            Plan::from_json(plan_json).map_err(PyErr::from)?
        };

        let app = if plan.app_name.is_empty() {
            app_name.clone()
        } else {
            plan.app_name.clone()
        };

        let migration = MigrationController::new(&app);

        Ok(SkaalMesh {
            plan,
            registry: AgentRegistry::new(),
            state: InMemoryStateStore::new(),
            migration,
            channel: MeshChannel::new(),
        })
    }

    // ── Properties ────────────────────────────────────────────────────────────

    /// Application name.
    #[getter]
    pub fn app_name(&self) -> &str {
        &self.plan.app_name
    }

    // ── Agent Registry ────────────────────────────────────────────────────────

    /// Register a new agent instance.
    ///
    /// Args:
    ///     agent_type:    Class name, e.g. ``"Customer"``.
    ///     agent_id:      Unique identity key, e.g. ``"customer-123"``.
    ///     instance:      Replica index (0-based, default 0).
    ///     metadata_json: Optional JSON object with extra fields.
    ///
    /// Returns:
    ///     JSON string representing the created :class:`AgentEntry`.
    ///
    /// Raises:
    ///     RuntimeError: If the agent ID is already registered.
    #[pyo3(signature = (agent_type, agent_id, instance = 0, metadata_json = None))]
    pub fn register_agent(
        &self,
        agent_type: &str,
        agent_id: &str,
        instance: u32,
        metadata_json: Option<&str>,
    ) -> PyResult<String> {
        let metadata = metadata_json
            .map(|s| serde_json::from_str::<Value>(s))
            .transpose()
            .map_err(|e| MeshError::SerdeError(e.to_string()))?;

        let entry = self
            .registry
            .register(agent_type, agent_id, instance, metadata)
            .map_err(PyErr::from)?;

        serde_json::to_string(&entry).map_err(|e| PyErr::from(MeshError::SerdeError(e.to_string())))
    }

    /// Update the status of a registered agent.
    ///
    /// Args:
    ///     agent_id: The agent's unique ID.
    ///     status:   One of ``"starting"``, ``"running"``, ``"idle"``,
    ///               ``"stopping"``, ``"stopped"``, ``"error"``.
    ///
    /// Raises:
    ///     ValueError: If the status string is not recognised.
    ///     KeyError:   If the agent ID is not registered.
    pub fn update_agent_status(&self, agent_id: &str, status: &str) -> PyResult<()> {
        let s = AgentStatus::from_str(status).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "Unknown status {status:?}. Valid values: \
                 starting, running, idle, stopping, stopped, error"
            ))
        })?;
        self.registry.update_status(agent_id, s).map_err(PyErr::from)
    }

    /// Deregister an agent.  No-op if the ID does not exist.
    pub fn deregister_agent(&self, agent_id: &str) -> PyResult<()> {
        self.registry.deregister(agent_id).map_err(PyErr::from)
    }

    /// Look up an agent by ID.
    ///
    /// Returns:
    ///     JSON string for the agent, or ``None`` if not found.
    pub fn get_agent(&self, agent_id: &str) -> PyResult<Option<String>> {
        match self.registry.get(agent_id).map_err(PyErr::from)? {
            None => Ok(None),
            Some(e) => Ok(Some(
                serde_json::to_string(&e)
                    .map_err(|e| MeshError::SerdeError(e.to_string()))?,
            )),
        }
    }

    /// List agents, optionally filtered.
    ///
    /// Args:
    ///     agent_type: Filter by type name (``None`` = all types).
    ///     status:     Filter by status string (``None`` = all statuses).
    ///
    /// Returns:
    ///     JSON array string of agent records.
    #[pyo3(signature = (agent_type = None, status = None))]
    pub fn list_agents(
        &self,
        agent_type: Option<&str>,
        status: Option<&str>,
    ) -> PyResult<String> {
        let agents = self
            .registry
            .list(agent_type, status)
            .map_err(PyErr::from)?;
        serde_json::to_string(&agents).map_err(|e| PyErr::from(MeshError::SerdeError(e.to_string())))
    }

    /// Route a call to an agent and return routing metadata as JSON.
    ///
    /// This method validates that the agent exists and is not stopped/errored,
    /// then marks it as ``"running"`` and returns a routing descriptor.  The
    /// actual Python method dispatch is performed by the caller using the
    /// returned metadata.
    ///
    /// Args:
    ///     agent_type: Agent class name.
    ///     agent_id:   Agent identity key.
    ///     method:     Method name to invoke.
    ///     args_json:  JSON object of keyword arguments (default ``"{}"``) .
    ///
    /// Returns:
    ///     JSON object ``{"status": "routed", "agent_id": "…", …}``.
    ///
    /// Raises:
    ///     KeyError:   If the agent is not registered or has stopped.
    #[pyo3(signature = (agent_type, agent_id, method, args_json = "{}"))]
    pub fn route_agent_call(
        &self,
        agent_type: &str,
        agent_id: &str,
        method: &str,
        args_json: &str,
    ) -> PyResult<String> {
        // Validate args are parseable JSON
        let _args: Value = serde_json::from_str(args_json)
            .map_err(|e| MeshError::SerdeError(e.to_string()))?;

        // Look up agent in registry
        let entry = self
            .registry
            .get(agent_id)
            .map_err(PyErr::from)?
            .ok_or_else(|| MeshError::AgentNotFound {
                agent_type: agent_type.to_owned(),
                agent_id: agent_id.to_owned(),
            })?;

        if entry.status == AgentStatus::Stopped || entry.status == AgentStatus::Error {
            return Err(PyErr::from(MeshError::AgentNotFound {
                agent_type: agent_type.to_owned(),
                agent_id: agent_id.to_owned(),
            }));
        }

        // Mark as running
        let _ = self
            .registry
            .update_status(agent_id, AgentStatus::Running);

        let result = serde_json::json!({
            "status": "routed",
            "agent_type": agent_type,
            "agent_id": agent_id,
            "method": method,
            "node": "localhost",
        });

        Ok(result.to_string())
    }

    // ── State Store ───────────────────────────────────────────────────────────

    /// Get a value from the shared state store.
    ///
    /// Returns:
    ///     JSON string for the value, or ``None`` if the key does not exist.
    pub fn state_get(&self, key: &str) -> PyResult<Option<String>> {
        match self.state.get(key).map_err(PyErr::from)? {
            None => Ok(None),
            Some(v) => Ok(Some(
                serde_json::to_string(&v)
                    .map_err(|e| MeshError::SerdeError(e.to_string()))?,
            )),
        }
    }

    /// Set a value in the shared state store.
    ///
    /// Args:
    ///     key:        Arbitrary string key.
    ///     value_json: JSON-encoded value to store.
    pub fn state_set(&self, key: &str, value_json: &str) -> PyResult<()> {
        let value: Value = serde_json::from_str(value_json)
            .map_err(|e| MeshError::SerdeError(e.to_string()))?;
        self.state.set(key, value).map_err(PyErr::from)
    }

    /// Delete a key from the state store.  No-op if the key does not exist.
    pub fn state_delete(&self, key: &str) -> PyResult<()> {
        self.state.delete(key).map_err(PyErr::from)
    }

    /// Check whether a key exists in the state store.
    pub fn state_exists(&self, key: &str) -> PyResult<bool> {
        self.state.exists(key).map_err(PyErr::from)
    }

    /// Return all keys that start with `prefix` (sorted).
    #[pyo3(signature = (prefix = ""))]
    pub fn state_keys(&self, prefix: &str) -> PyResult<Vec<String>> {
        self.state.keys_with_prefix(prefix).map_err(PyErr::from)
    }

    // ── Migration ─────────────────────────────────────────────────────────────

    /// Start a zero-downtime migration for a storage variable.
    ///
    /// Args:
    ///     variable_name:  Fully-qualified storage name, e.g. ``"counter.Counts"``.
    ///     source_backend: Current backend name, e.g. ``"sqlite"``.
    ///     target_backend: Target backend name, e.g. ``"redis"``.
    ///
    /// Returns:
    ///     JSON string of the initial :class:`MigrationState` (stage 1).
    ///
    /// Raises:
    ///     ValueError: If a non-complete migration already exists for this variable.
    pub fn start_migration(
        &self,
        variable_name: &str,
        source_backend: &str,
        target_backend: &str,
    ) -> PyResult<String> {
        let state = self
            .migration
            .start(variable_name, source_backend, target_backend)
            .map_err(PyErr::from)?;
        serde_json::to_string(&state).map_err(|e| PyErr::from(MeshError::SerdeError(e.to_string())))
    }

    /// Advance a migration to its next stage.
    ///
    /// Args:
    ///     variable_name:     Storage variable name.
    ///     discrepancy_count: Number of read discrepancies observed in this stage.
    ///     keys_migrated:     Number of keys copied during this stage.
    ///
    /// Returns:
    ///     JSON string of the updated :class:`MigrationState`.
    #[pyo3(signature = (variable_name, discrepancy_count = 0, keys_migrated = 0))]
    pub fn advance_migration(
        &self,
        variable_name: &str,
        discrepancy_count: u64,
        keys_migrated: u64,
    ) -> PyResult<String> {
        let state = self
            .migration
            .advance(variable_name, discrepancy_count, keys_migrated)
            .map_err(PyErr::from)?;
        serde_json::to_string(&state).map_err(|e| PyErr::from(MeshError::SerdeError(e.to_string())))
    }

    /// Roll back a migration by one stage.
    ///
    /// Returns:
    ///     JSON string of the updated :class:`MigrationState`.
    pub fn rollback_migration(&self, variable_name: &str) -> PyResult<String> {
        let state = self
            .migration
            .rollback(variable_name)
            .map_err(PyErr::from)?;
        serde_json::to_string(&state).map_err(|e| PyErr::from(MeshError::SerdeError(e.to_string())))
    }

    /// Get the current migration state for a variable.
    ///
    /// Returns:
    ///     JSON string, or ``None`` if no migration exists for this variable.
    pub fn get_migration(&self, variable_name: &str) -> PyResult<Option<String>> {
        match self.migration.get(variable_name).map_err(PyErr::from)? {
            None => Ok(None),
            Some(s) => Ok(Some(
                serde_json::to_string(&s)
                    .map_err(|e| MeshError::SerdeError(e.to_string()))?,
            )),
        }
    }

    /// List all migrations (active and completed).
    ///
    /// Returns:
    ///     JSON array string.
    pub fn list_migrations(&self) -> PyResult<String> {
        let states = self.migration.list_all().map_err(PyErr::from)?;
        serde_json::to_string(&states).map_err(|e| PyErr::from(MeshError::SerdeError(e.to_string())))
    }

    // ── Channels ──────────────────────────────────────────────────────────────

    /// Publish a message to a topic.
    ///
    /// Args:
    ///     topic:        Topic name string.
    ///     payload_json: JSON-encoded message payload.
    ///
    /// Returns:
    ///     Number of active receivers that received the message.
    pub fn publish(&self, topic: &str, payload_json: &str) -> PyResult<usize> {
        let payload: Value = serde_json::from_str(payload_json)
            .map_err(|e| MeshError::SerdeError(e.to_string()))?;
        self.channel.publish(topic, payload).map_err(PyErr::from)
    }

    // ── Health ────────────────────────────────────────────────────────────────

    /// Return a JSON health snapshot of the entire mesh.
    ///
    /// The snapshot includes agent counts by status, state-store key count,
    /// active migration count, and channel topic count.
    pub fn health_snapshot(&self) -> PyResult<String> {
        let snapshot = serde_json::json!({
            "app": self.plan.app_name,
            "status": "ok",
            "agents": {
                "total": self.registry.count(),
                "running": self.registry.count_by_status("running"),
                "idle":    self.registry.count_by_status("idle"),
                "error":   self.registry.count_by_status("error"),
            },
            "state": {
                "keys": self.state.len(),
            },
            "migrations": {
                "active": self.migration.count_active(),
            },
            "channels": {
                "topics": self.channel.topic_count(),
            },
        });
        Ok(snapshot.to_string())
    }

    /// Return the solved plan as a JSON string.
    pub fn plan_json(&self) -> PyResult<String> {
        serde_json::to_string(&self.plan).map_err(|e| PyErr::from(MeshError::SerdeError(e.to_string())))
    }

    // ── __repr__ ──────────────────────────────────────────────────────────────

    pub fn __repr__(&self) -> String {
        format!(
            "SkaalMesh(app={:?}, agents={}, state_keys={}, migrations={})",
            self.plan.app_name,
            self.registry.count(),
            self.state.len(),
            self.migration.count_active(),
        )
    }
}

// ── Module entry point ─────────────────────────────────────────────────────────

/// Python module `skaal_mesh`.
#[pymodule]
fn skaal_mesh(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<SkaalMesh>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
