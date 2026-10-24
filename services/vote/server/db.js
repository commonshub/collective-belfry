// SQLite storage (node:sqlite, one file in a volume). The vote service is the only writer.
import { DatabaseSync } from "node:sqlite"

const SCHEMA = `
CREATE TABLE IF NOT EXISTS registrations (
  pass_id INTEGER PRIMARY KEY,
  commitment TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS polls (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  options TEXT NOT NULL,
  creator_pass_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  closes_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS poll_members (
  poll_id TEXT NOT NULL REFERENCES polls(id),
  pass_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  commitment TEXT NOT NULL,
  PRIMARY KEY (poll_id, pass_id),
  UNIQUE (poll_id, idx)
);
CREATE TABLE IF NOT EXISTS poll_roots (
  poll_id TEXT NOT NULL REFERENCES polls(id),
  root TEXT NOT NULL,
  PRIMARY KEY (poll_id, root)
);
CREATE TABLE IF NOT EXISTS votes (
  poll_id TEXT NOT NULL REFERENCES polls(id),
  nullifier TEXT NOT NULL,
  option INTEGER NOT NULL,
  proof TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (poll_id, nullifier)
);
`

export function openDb(path) {
  const db = new DatabaseSync(path)
  db.exec("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;")
  db.exec(SCHEMA)
  return db
}

/** Runs fn inside a transaction (node:sqlite has no helper). */
export function tx(db, fn) {
  db.exec("BEGIN IMMEDIATE")
  try {
    const result = fn()
    db.exec("COMMIT")
    return result
  } catch (err) {
    db.exec("ROLLBACK")
    throw err
  }
}
