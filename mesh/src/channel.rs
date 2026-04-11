//! In-process pub/sub channels for inter-function communication.
//!
//! `MeshChannel` uses `tokio::sync::broadcast` internally so multiple
//! subscribers can each receive every published message.  Topics are
//! created lazily on first use.

use crate::error::MeshError;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::sync::broadcast;

/// Default capacity per broadcast channel (messages).
const CHANNEL_CAPACITY: usize = 1024;

// ── Message ────────────────────────────────────────────────────────────────────

/// A single pub/sub message.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub topic: String,
    pub payload: Value,
    pub timestamp: String,
}

// ── MeshChannel ────────────────────────────────────────────────────────────────

/// Thread-safe, multi-topic pub/sub channel.
///
/// Cloning gives a second handle to the same set of topics.
#[derive(Clone)]
pub struct MeshChannel {
    /// topic → broadcast sender
    channels: Arc<Mutex<HashMap<String, broadcast::Sender<Message>>>>,
}

impl Default for MeshChannel {
    fn default() -> Self {
        Self {
            channels: Arc::new(Mutex::new(HashMap::new())),
        }
    }
}

impl MeshChannel {
    pub fn new() -> Self {
        Self::default()
    }

    // ── Private helpers ──────────────────────────────────────────────────────

    fn sender_for(&self, topic: &str) -> Result<broadcast::Sender<Message>, MeshError> {
        let mut map = self
            .channels
            .lock()
            .map_err(|_| MeshError::ChannelError("channel lock poisoned".into()))?;

        if let Some(tx) = map.get(topic) {
            return Ok(tx.clone());
        }
        let (tx, _) = broadcast::channel(CHANNEL_CAPACITY);
        map.insert(topic.to_owned(), tx.clone());
        Ok(tx)
    }

    // ── Public API ───────────────────────────────────────────────────────────

    /// Publish `payload` to `topic`.  Returns the number of active receivers.
    ///
    /// If there are no subscribers the message is silently dropped (consistent
    /// with broadcast channel semantics).
    pub fn publish(&self, topic: &str, payload: Value) -> Result<usize, MeshError> {
        let tx = self.sender_for(topic)?;
        let msg = Message {
            topic: topic.to_owned(),
            payload,
            timestamp: Utc::now().to_rfc3339(),
        };
        // `send` returns Err when there are no receivers — that's fine.
        Ok(tx.send(msg).unwrap_or(0))
    }

    /// Subscribe to `topic`.  Returns a `broadcast::Receiver` for the caller
    /// to poll.  Each subscriber gets every message published after this call.
    pub fn subscribe(&self, topic: &str) -> Result<broadcast::Receiver<Message>, MeshError> {
        let tx = self.sender_for(topic)?;
        Ok(tx.subscribe())
    }

    /// Number of distinct topics that have been used.
    pub fn topic_count(&self) -> usize {
        self.channels.lock().map(|m| m.len()).unwrap_or(0)
    }

    /// Number of live receivers on a topic (0 if topic does not exist).
    pub fn receiver_count(&self, topic: &str) -> usize {
        self.channels
            .lock()
            .ok()
            .and_then(|m| m.get(topic).map(|tx| tx.receiver_count()))
            .unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn publish_to_no_subscribers_is_ok() {
        let ch = MeshChannel::new();
        // No error, no panic
        let receivers = ch.publish("events", json!({"type": "ping"})).unwrap();
        assert_eq!(receivers, 0);
    }

    #[test]
    fn subscribe_then_publish() {
        let ch = MeshChannel::new();
        let mut rx = ch.subscribe("events").unwrap();
        ch.publish("events", json!({"type": "ping"})).unwrap();

        let msg = rx.try_recv().expect("should have a message");
        assert_eq!(msg.topic, "events");
        assert_eq!(msg.payload["type"], "ping");
    }

    #[test]
    fn multiple_topics_independent() {
        let ch = MeshChannel::new();
        let mut rx_a = ch.subscribe("a").unwrap();
        let mut rx_b = ch.subscribe("b").unwrap();

        ch.publish("a", json!(1)).unwrap();
        ch.publish("b", json!(2)).unwrap();

        assert_eq!(rx_a.try_recv().unwrap().payload, json!(1));
        assert_eq!(rx_b.try_recv().unwrap().payload, json!(2));
        // Cross-channel: rx_a should not see "b" messages
        assert!(rx_a.try_recv().is_err());
    }
}
