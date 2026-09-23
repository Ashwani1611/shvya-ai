# Hosted WhatsApp gateway sharding

Hosted accounts are assigned to a durable `hosted_gateway_shard`. Assignment uses rendezvous hashing only for an unassigned account; after persistence, configuration order or fleet growth cannot silently move it. Configure a fleet with `WHATSAPP_WEB_GATEWAYS`, for example `{"east":"http://gateway-east:3000","west":"http://gateway-west:3000"}`. A legacy single URL remains shard `primary`.

Each gateway also has a stable deployment-supplied `WHATSAPP_WEB_GATEWAY_INSTANCE_ID`, its shard name, and `WHATSAPP_WEB_MAX_SESSIONS`. Redis DB 4 provides the fast distributed session lock. PostgreSQL stores the observed owner, lease expiry, heartbeat, and session state. Both layers matter: Redis fences concurrent live ownership; PostgreSQL survives restarts and gives the control plane durable health/routing state.

Every callback includes shard, owner, and lease expiry and is authenticated by the existing callback token. Django rejects a callback from a shard other than the account's durable assignment. Provider/message IDs remain the message idempotency boundary. History events remain marked as history and do not enter the live-AI path.

## Rebalance and recovery

1. Stop new sessions on the source shard and confirm the target has capacity.
2. Log out/drain the account or wait until the durable lease expires. Never copy or mount a live session directory on two gateways.
3. Call the controlled `move_hosted_account` operation in an authenticated administrative path or shell. It locks the account row and refuses an active lease.
4. Move/restore the one account's session material according to the gateway storage runbook, with only the target mounted read-write.
5. Start the target session. Confirm callback shard/owner, heartbeat, QR/connected state, history boundary, and a deduplicated test send.
6. Roll back by draining again, waiting for lease expiry, and moving the durable shard back. Never edit the database while a gateway still owns the lease.

On gateway loss, wait for the Redis/durable lease to expire before recovery. A replacement using the same shard and restored session volume can claim the account. If the volume is lost, the customer must re-link by QR; CRM messages and account configuration remain in PostgreSQL. The gateway rejects new sessions at its hard cap with 503, rather than creating unbounded Chromium processes.

Monitor sessions/capacity, RSS, CPU, reconnects, failed callbacks, history sync failures, and lease conflicts from `/health`; alert before 80% of the configured cap. The value 50 is only a default safety cap, not a tested per-host capacity. Establish the real cap by gradually loading staging while measuring browser RSS/CPU, QR/ready time, reconnect stability, and message latency.
