//! Migration controller — in-memory state machine for the 6-stage
//! zero-downtime backend migration protocol.
//!
//! This mirrors the Python `MigrationEngine` but runs inside the mesh so
//! that all nodes share a single view of migration progress.

use crate::error::MeshError;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

// ── Stage names ────────────────────────────────────────────────────────────────

/// Human-readable name for each migration stage (index = stage number).
pub const STAGE_NAMES: &[&str] = &[
    "idle",         // 0
    "shadow_write", // 1 — writes go to both; reads from source
    "shadow_read",  // 2 — reads from source+target (verify); writes to both
    "dual_read",    // 3 — reads from target (fallback source); writes to both
    "new_primary",  // 4 — reads and writes go to target only
    "cleanup",      // 5 — source being drained
    "done",         // 6 — migration complete
];

fn stage_name(stage: u8) -> &'static str {
    STAGE_NAMES.get(stage as usize).copied().unwrap_or("unknown")
}

// ── MigrationState ─────────────────────────────────────────────────────────────

/// Snapshot of a single storage variable's migration progress.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MigrationState {
    pub variable_name: String,
    pub source_backend: String,
    pub target_backend: String,
    /// Current stage: 0 (idle) → 6 (done).
    pub stage: u8,
    /// Name of the current stage, e.g. `"shadow_write"`.
    pub stage_name: String,
    pub started_at: String,
    pub advanced_at: String,
    pub discrepancy_count: u64,
    pub keys_migrated: u64,
    pub app_name: String,
}

impl MigrationState {
    pub fn is_complete(&self) -> bool {
        self.stage >= 6
    }
}

// ── MigrationController ────────────────────────────────────────────────────────

/// Thread-safe in-memory migration state machine.
///
/// All clones share the same data via `Arc`.
#[derive(Clone)]
pub struct MigrationController {
    migrations: Arc<RwLock<HashMap<String, MigrationState>>>,
    app_name: String,
}

impl MigrationController {
    pub fn new(app_name: &str) -> Self {
        Self {
            migrations: Arc::new(RwLock::new(HashMap::new())),
            app_name: app_name.to_owned(),
        }
    }

    // ── State transitions ────────────────────────────────────────────────────

    /// Begin a new migration at stage 1 (`shadow_write`).
    ///
    /// Returns `MigrationError` if a non-complete migration already exists for
    /// this variable.
    pub fn start(
        &self,
        variable_name: &str,
        source_backend: &str,
        target_backend: &str,
    ) -> Result<MigrationState, MeshError> {
        let mut map = self
            .migrations
            .write()
            .map_err(|_| MeshError::MigrationError("lock poisoned".into()))?;

        if let Some(existing) = map.get(variable_name) {
            if !existing.is_complete() {
                return Err(MeshError::MigrationError(format!(
                    "Migration for '{variable_name}' already in progress at stage {} ({})",
                    existing.stage, existing.stage_name
                )));
            }
        }

        let now = Utc::now().to_rfc3339();
        let state = MigrationState {
            variable_name: variable_name.to_owned(),
            source_backend: source_backend.to_owned(),
            target_backend: target_backend.to_owned(),
            stage: 1,
            stage_name: stage_name(1).to_owned(),
            started_at: now.clone(),
            advanced_at: now,
            discrepancy_count: 0,
            keys_migrated: 0,
            app_name: self.app_name.clone(),
        };
        map.insert(variable_name.to_owned(), state.clone());
        Ok(state)
    }

    /// Advance to the next stage.  Accumulates `discrepancy_count`.
    pub fn advance(
        &self,
        variable_name: &str,
        discrepancy_count: u64,
        keys_migrated: u64,
    ) -> Result<MigrationState, MeshError> {
        let mut map = self
            .migrations
            .write()
            .map_err(|_| MeshError::MigrationError("lock poisoned".into()))?;

        let state = map
            .get_mut(variable_name)
            .ok_or_else(|| MeshError::MigrationError(
                format!("No migration in progress for '{variable_name}'"),
            ))?;

        if state.stage >= 6 {
            return Err(MeshError::MigrationError(format!(
                "Migration for '{variable_name}' is already complete"
            )));
        }

        state.stage += 1;
        state.stage_name = stage_name(state.stage).to_owned();
        state.advanced_at = Utc::now().to_rfc3339();
        state.discrepancy_count += discrepancy_count;
        state.keys_migrated += keys_migrated;
        Ok(state.clone())
    }

    /// Roll back one stage.  Cannot roll back from stage 0 or a completed migration.
    pub fn rollback(&self, variable_name: &str) -> Result<MigrationState, MeshError> {
        let mut map = self
            .migrations
            .write()
            .map_err(|_| MeshError::MigrationError("lock poisoned".into()))?;

        let state = map
            .get_mut(variable_name)
            .ok_or_else(|| MeshError::MigrationError(
                format!("No migration in progress for '{variable_name}'"),
            ))?;

        if state.stage == 0 {
            return Err(MeshError::MigrationError(
                "Already at initial stage — cannot roll back further".into(),
            ));
        }
        if state.stage >= 6 {
            return Err(MeshError::MigrationError(
                "Cannot roll back a completed migration".into(),
            ));
        }

        state.stage -= 1;
        state.stage_name = stage_name(state.stage).to_owned();
        state.advanced_at = Utc::now().to_rfc3339();
        Ok(state.clone())
    }

    // ── Queries ──────────────────────────────────────────────────────────────

    /// Look up migration state for one variable.
    pub fn get(&self, variable_name: &str) -> Result<Option<MigrationState>, MeshError> {
        let map = self
            .migrations
            .read()
            .map_err(|_| MeshError::MigrationError("lock poisoned".into()))?;
        Ok(map.get(variable_name).cloned())
    }

    /// All migrations (including completed ones).
    pub fn list_all(&self) -> Result<Vec<MigrationState>, MeshError> {
        let map = self
            .migrations
            .read()
            .map_err(|_| MeshError::MigrationError("lock poisoned".into()))?;
        Ok(map.values().cloned().collect())
    }

    /// Active (non-complete) migrations only.
    pub fn list_active(&self) -> Result<Vec<MigrationState>, MeshError> {
        Ok(self
            .list_all()?
            .into_iter()
            .filter(|s| !s.is_complete())
            .collect())
    }

    /// Number of active migrations.
    pub fn count_active(&self) -> usize {
        self.list_active().map(|v| v.len()).unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_migration_cycle() {
        let ctrl = MigrationController::new("myapp");

        // Start
        let s = ctrl.start("counter.Counts", "sqlite", "redis").unwrap();
        assert_eq!(s.stage, 1);
        assert_eq!(s.stage_name, "shadow_write");

        // Advance through all stages
        for expected in 2..=6u8 {
            let s = ctrl.advance("counter.Counts", 0, 10).unwrap();
            assert_eq!(s.stage, expected);
            assert_eq!(s.stage_name, STAGE_NAMES[expected as usize]);
        }

        let s = ctrl.get("counter.Counts").unwrap().unwrap();
        assert!(s.is_complete());
        assert_eq!(s.keys_migrated, 50);
    }

    #[test]
    fn rollback() {
        let ctrl = MigrationController::new("myapp");
        ctrl.start("v", "a", "b").unwrap();
        ctrl.advance("v", 0, 0).unwrap(); // stage 2
        let s = ctrl.rollback("v").unwrap();
        assert_eq!(s.stage, 1);
    }

    #[test]
    fn duplicate_start_fails() {
        let ctrl = MigrationController::new("myapp");
        ctrl.start("v", "a", "b").unwrap();
        assert!(ctrl.start("v", "a", "b").is_err());
    }

    #[test]
    fn advance_beyond_done_fails() {
        let ctrl = MigrationController::new("myapp");
        ctrl.start("v", "a", "b").unwrap();
        for _ in 0..5 {
            ctrl.advance("v", 0, 0).unwrap();
        }
        assert!(ctrl.advance("v", 0, 0).is_err());
    }
}
