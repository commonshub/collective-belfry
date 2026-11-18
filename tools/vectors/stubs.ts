// Stand-ins for the browser modules core/model.ts reads, so it runs in Node against the vectors.
const g = globalThis as unknown as { __EVENTS: unknown[]; __ME: string }
export const all = () => g.__EVENTS
export const touch = () => {}
export const useVersion = () => 0
export type Ev = { id: string; pubkey: string; kind: number; created_at: number; tags: string[][]; content: string; sig: string; pending?: unknown }
export const me = () => g.__ME
export const setWriteGuard = () => {}
export const belfryAdminKeys = (): string[] => []
export const isAdmin = () => false
export const BELFRY = { groupsRelay: 'wss://groups-relay.example.org', mainRelay: 'wss://relay.example.org', domain: 'example.org' }
export const LABEL_NS = 'osv/member-label'
export const THEME = 'osv'
export const getBackend = () => 'demo'
export const TZ = 'UTC'
