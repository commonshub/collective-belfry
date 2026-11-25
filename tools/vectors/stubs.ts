// Stand-ins for the browser modules core/model.ts reads, so it runs in Node against the vectors.
const g = globalThis as unknown as { __EVENTS: unknown[]; __ME: string }
export const all = () => g.__EVENTS
export const touch = () => {}
export const useVersion = () => 0
export type Ev = { id: string; pubkey: string; kind: number; created_at: number; tags: string[][]; content: string; sig: string; pending?: unknown }
export const me = () => g.__ME
export const setWriteGuard = () => {}
export const belfryAdminKeys = (): string[] => []
export const belfryGroupsKey = (): string => ''
export const isAdmin = () => false
export const agentOwner = (_: string): string | undefined => undefined
export const queryOnce = async () => []
export const isLocalOnly = () => true
export const BELFRY = { groupsRelay: 'wss://groups-relay.example.org', mainRelay: 'wss://relay.example.org', domain: 'example.org' }
export const LABEL_NS = 'osv/member-label'
export const THEME = 'osv'
export const getBackend = () => 'demo'
export const TZ = 'UTC'
// guestjoin.ts (guest-signin.json)
export const replaceHash = () => {}
export const guestInfo = () => null
export const setGuestInfo = () => {}
export const demoSession = (email: string) => ({ backend: 'demo', email, clientSecret: '', membership: { status: 'active' } })
export const guestChanged = () => {}
export const addEvents = () => {}
export const signedFetch = async () => { throw new Error('no network') }
export const demoRoles = () => {}
export const setDemoRole = () => {}
// format 3 (enc): what model.ts asks of sync / access
export const groupWrapReady = () => false
export const setPrivateVerdict = (_f: unknown) => {}
export const encryptedFormat = (): 1 | 3 | undefined => 1
export const encSeen = () => false
export const roleOf = (_pk: string): undefined => undefined // (vectors: the history decides)
export const agentGivenName = (_pk: string): string | undefined => undefined
export const queryComplete = async (..._a: unknown[]): Promise<null> => null
