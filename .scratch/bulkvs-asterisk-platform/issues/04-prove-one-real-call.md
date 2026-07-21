# Stand up BulkVS↔Asterisk and prove one real inbound call via ARI

Type: task
Status: open
Blocked by: 01, 02

## Question

Not a decision — the enabling task that grounds every infra/data-model choice in reality. Get **one real
inbound call** to a BulkVS DID to land on the native Asterisk, enter a Stasis app, be answered + recorded
under ARI control, and hang up cleanly. Then the reverse: originate **one outbound** call via ARI.

Drives out the true SIP/NAT/codec/firewall requirements that tickets 05, 06, and 09 depend on. Agent drives
what it can over the `dispatch` ssh alias; otherwise hands the user a precise, ordered checklist (trunk config,
allow-listed IPs, ARI user, dialplan hand-off, test-call steps).

## Answer

<!-- record what was done + resulting facts (working config paths, IPs, ARI creds location, gotchas) -->
