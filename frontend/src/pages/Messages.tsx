// Placeholder route (Ticket 06 shell). The real SMS inbox arrives in a later ticket.
export default function Messages() {
  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Messages</h2>
      <div className="card">
        <div className="placeholder">
          <div style={{ fontSize: 32, marginBottom: 8 }}>💬</div>
          <div>SMS inbox coming soon.</div>
          <div className="muted" style={{ marginTop: 6 }}>
            Two-way messaging for platform (BulkVS/Asterisk) numbers will live here.
          </div>
        </div>
      </div>
    </div>
  );
}
