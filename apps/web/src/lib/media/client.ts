import "server-only"

import { correlateLiteCall } from "@/lib/http/lite-diagnostics"
import { mediaCacheControl } from "@/lib/media/cache"
import { trustedAuthorHeaders } from "@/lib/social/client"

export class MediaServiceConfigurationError extends Error {
  constructor(message = "MEDIA_SERVICE_URL or THRESHOLD_INTERNAL_TOKEN is not configured") {
    super(message)
    this.name = "MediaServiceConfigurationError"
  }
}

function baseUrl(): string {
  const value = process.env.MEDIA_SERVICE_URL
  if (!value?.trim()) throw new MediaServiceConfigurationError()
  const base = value.replace(/\/$/, "")
  const litePort = process.env.PERLIMEN_LITE_API_PORT
  if (litePort !== undefined) {
    const port = Number(litePort)
    if (String(port) !== litePort || !Number.isInteger(port) || port < 1024 || port > 65535 ||
        base !== `http://127.0.0.1:${port}/media`) {
      throw new MediaServiceConfigurationError("Lite media service must use its configured loopback endpoint")
    }
  }
  return base
}

function internalToken(): string {
  const value = process.env.THRESHOLD_INTERNAL_TOKEN
  if (!value?.trim()) throw new MediaServiceConfigurationError()
  return value
}

export async function uploadMediaAsset(request: Request): Promise<Response> {
  const userHeaders = await trustedAuthorHeaders()
  if (!userHeaders) return Response.json({ error: "unauthenticated" }, { status: 401 })

  const headers = new Headers({
    "X-Threshold-Internal-Token": internalToken(),
    ...userHeaders,
  })
  await correlateLiteCall(headers)
  for (const name of ["content-type", "content-length"]) {
    const value = request.headers.get(name)
    if (value !== null) headers.set(name, value)
  }
  const response = await fetch(`${baseUrl()}/v1/assets/upload`, {
    method: "POST",
    headers,
    body: request.body,
    duplex: "half",
    cache: "no-store",
    redirect: process.env.PERLIMEN_LITE_API_PORT === undefined ? undefined : "error",
  } as RequestInit & { duplex: "half" })
  if (process.env.PERLIMEN_LITE_API_PORT !== undefined && response.status === 201) {
    const asset = await response.json()
    return typeof asset?.id === "string"
      ? Response.json({ id: asset.id }, { status: 201 })
      : Response.json({ error: "invalid upload response" }, { status: 502 })
  }
  return new Response(response.body, {
    status: response.status,
    headers: { "content-type": response.headers.get("content-type") ?? "application/json" },
  })
}

export async function fetchMediaAsset(path: string): Promise<Response> {
  const lite = process.env.PERLIMEN_LITE_API_PORT !== undefined
  const user = lite ? await trustedAuthorHeaders() : null
  const response = await fetch(`${baseUrl()}/media/assets/${path}`, {
    headers: { "X-Threshold-Internal-Token": internalToken(), ...user },
    cache: "no-store",
    redirect: lite ? "error" : undefined,
  })
  return new Response(response.body, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") ?? "application/octet-stream",
      "cache-control": lite ? "private, no-store" : mediaCacheControl(response.status),
    },
  })
}
