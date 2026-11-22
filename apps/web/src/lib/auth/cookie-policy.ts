/**
 * Pure (framework-free) cookie-bridge policy shared by the server-only
 * `cookies.ts`. Kept out of any module that imports `server-only` so it can be
 * unit-tested directly.
 */

export const SESSION_COOKIE = "threshold_session"
export const REFRESH_COOKIE = "threshold_refresh"
export const BFF_REFRESH_PATH = "/api/auth"

type ParsedSetCookie = {
  name: string
  value: string
  path: string
  maxAge?: number
  expires?: Date
}

function parseSetCookie(raw: string): ParsedSetCookie | null {
  const [pair, ...attrs] = raw.split(";")
  const eq = pair.indexOf("=")
  if (eq === -1) return null

  const name = pair.slice(0, eq).trim()
  const value = pair.slice(eq + 1).trim()
  if (!name) return null

  let path = "/"
  let maxAge: number | undefined
  let expires: Date | undefined

  for (const attr of attrs) {
    const sep = attr.indexOf("=")
    const key = (sep === -1 ? attr : attr.slice(0, sep)).trim().toLowerCase()
    const attrValue = sep === -1 ? "" : attr.slice(sep + 1).trim()
    if (key === "path") {
      path = attrValue || "/"
    } else if (key === "max-age") {
      const parsed = Number(attrValue)
      if (!Number.isNaN(parsed)) maxAge = parsed
    } else if (key === "expires") {
      const parsed = new Date(attrValue)
      if (!Number.isNaN(parsed.getTime())) expires = parsed
    }
  }

  return { name, value, path, maxAge, expires }
}

function isDeletion(parsed: ParsedSetCookie): boolean {
  return (
    parsed.value === "" ||
    parsed.maxAge === 0 ||
    (parsed.expires !== undefined && parsed.expires.getTime() <= Date.now())
  )
}

export type CookieMutation = {
  name: string
  value: string
  path: string
  httpOnly: true
  secure: boolean
  sameSite: "lax"
  maxAge?: number
}

/**
 * Pure transform of backend Set-Cookie headers into the mutations the BFF must
 * apply on the browser-facing origin: rewrites the refresh cookie path, applies
 * a single Secure/SameSite policy, and normalizes deletions to maxAge=0.
 */
export function planAuthCookieMutations(
  setCookieHeaders: string[],
  secure: boolean,
): CookieMutation[] {
  const mutations: CookieMutation[] = []
  for (const raw of setCookieHeaders) {
    const parsed = parseSetCookie(raw)
    if (!parsed) continue
    const path = parsed.name === REFRESH_COOKIE &&
      (parsed.path === "/v1/auth" || parsed.path === "/users/v1/auth")
      ? BFF_REFRESH_PATH
      : parsed.path

    if (isDeletion(parsed)) {
      mutations.push({
        name: parsed.name,
        value: "",
        path,
        httpOnly: true,
        secure,
        sameSite: "lax",
        maxAge: 0,
      })
      continue
    }

    mutations.push({
      name: parsed.name,
      value: parsed.value,
      path,
      httpOnly: true,
      secure,
      sameSite: "lax",
      ...(parsed.maxAge !== undefined ? { maxAge: parsed.maxAge } : {}),
    })
  }
  return mutations
}
