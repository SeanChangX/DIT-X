# Middleware notes: QoS, DDS, shared memory

## QoS: pick a profile from the contract

Mismatched or default QoS caused silent "sometimes it connects, sometimes it doesn't" bugs.
Every cross-domain topic therefore names one of the profiles in `contract.yaml`:

| profile | use for | why |
|---|---|---|
| `sensor` | poses, scans, detections, heartbeats | `best_effort` + `keep_last 1`: always the newest sample. A reliable stream retransmits old samples and delays the new ones behind them. |
| `state` | "current value" topics (startup status, navigation status) | `reliable` + `transient_local`: a node that starts late still gets the latest value immediately. |
| `command` | goals, mission commands | `reliable`, small queue: must not be dropped. |
| `stream` | high-rate streams read by third-party nodes (odometry for nav2) | `reliable`, depth 10: nav2 subscribes reliable, and a reliable subscriber never matches a `best_effort` publisher. |

Compatibility: a `reliable` subscriber does **not** match a `best_effort` publisher, and a
`transient_local` subscriber does not match a `volatile` publisher. Using the generated
`contract::X.qos()` on both sides avoids this. `ros2 topic info -v <topic>` shows both sides.

## DDS

* RMW: CycloneDDS everywhere (`RMW_IMPLEMENTATION`, `docker/cyclonedds.xml`).
* `ROS_DOMAIN_ID` comes from `config/robots.ini` (one per robot) and is never used to *select*
  config. Configuration is selected by the robot name.
* All containers use `network_mode: host` and `ipc: host`.

## Shared memory (check before assuming)

By default DDS between containers on one machine goes over the loopback network, **not** shared
memory. Large messages (images, point clouds) then cost a copy and a serialization each way.
Before optimizing, measure (`ros2 topic delay`/`hz`, netdata). If it matters:

* CycloneDDS + iceoryx (RouDi daemon as an extra compose service), or Fast DDS with its SHM
  transport (which needs `/dev/shm` shared: `ipc: host` already does that).
* Or run the heavy producer and consumer as **composable nodes in one process** (intra-process,
  zero copy). This is usually the simpler fix.
