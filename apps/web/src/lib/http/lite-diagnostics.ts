import { appendFile } from "node:fs/promises"
import { headers } from "next/headers"
import { after } from "next/server"

const requestIdHeader = "x-perlimen-lite-request-id"
const startHeader = "x-perlimen-lite-start-us"
const requestIdPattern = /^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/

type Route = "/app" | "/api/events/[slug]/check-in" | "/api/media/assets"

function nowUs(): number {
  return Math.round((performance.timeOrigin + performance.now()) * 1000)
}

// Only Proxy may stamp these headers. Never trust incoming values.
export function stampLiteRequest(request: Request, pathname: string): Headers | undefined {
  if (!process.env.PERLIMEN_LITE_BFF_LOG) return
  const measured = pathname === "/app"
    ? request.method === "GET" || request.method === "HEAD"
    : request.method === "POST" && (
      pathname === "/api/media/assets" || /^\/api\/events\/[^/]+\/check-in$/.test(pathname)
    )
  if (!measured && !request.headers.has(requestIdHeader) && !request.headers.has(startHeader)) return

  const incoming = new Headers(request.headers)
  incoming.delete(requestIdHeader)
  incoming.delete(startHeader)
  if (measured) {
    incoming.set(requestIdHeader, crypto.randomUUID().replaceAll("-", ""))
    incoming.set(startHeader, String(nowUs()))
  }
  return incoming
}

function readRequestId(incoming: Headers): string | null {
  const id = incoming.get(requestIdHeader)
  return id?.length === 32 && requestIdPattern.test(id) ? id : null
}

export async function correlateLiteCall(outgoing: Headers): Promise<void> {
  if (!process.env.PERLIMEN_LITE_BFF_LOG) return
  const id = readRequestId(await headers())
  if (id) outgoing.set("x-request-id", id)
}

export async function observeLiteResponse(route: Route, incoming?: Headers): Promise<void> {
  const log = process.env.PERLIMEN_LITE_BFF_LOG
  if (!log) return
  incoming ??= await headers()
  const requestId = readRequestId(incoming)
  const started = Number(incoming.get(startHeader))
  if (!requestId || !Number.isSafeInteger(started) || started <= 0) return

  // Proxy's own `after` ends at the proxy response, not the streamed App Router response.
  after(() => {
    const finished = nowUs()
    return appendFile(log, JSON.stringify({
      event: "http_request",
      service: "bff",
      request_id: requestId,
      route,
      started_at_unix_us: started,
      finished_at_unix_us: finished,
      duration_ms: Math.max(0, finished - started) / 1000,
    }) + "\n", { mode: 0o600 })
  })
}