import { afterEach, describe, expect, it } from "bun:test"
import { NextRequest } from "next/server"

import { proxy } from "@/proxy"

const environment = process.env as unknown as Record<string, string | undefined>
const originalEnvironment = {
  nodeEnv: process.env.NODE_ENV,
  authCookieSecure: process.env.AUTH_COOKIE_SECURE,
  trustedLanHttp: process.env.WEB_TRUSTED_LAN_HTTP,
  liteBffLog: process.env.PERLIMEN_LITE_BFF_LOG,
}

function restoreEnvironment(): void {
  environment.NODE_ENV = originalEnvironment.nodeEnv
  process.env.AUTH_COOKIE_SECURE = originalEnvironment.authCookieSecure
  process.env.WEB_TRUSTED_LAN_HTTP = originalEnvironment.trustedLanHttp
  if (originalEnvironment.liteBffLog === undefined) delete process.env.PERLIMEN_LITE_BFF_LOG
  else process.env.PERLIMEN_LITE_BFF_LOG = originalEnvironment.liteBffLog
}

afterEach(restoreEnvironment)

describe("proxy security configuration", () => {
  const request = new NextRequest("http://localhost/")
  it("validates cookie security on every request", () => {
    environment.NODE_ENV = "development"
    delete process.env.AUTH_COOKIE_SECURE
    delete process.env.WEB_TRUSTED_LAN_HTTP
    expect(proxy(request).status).toBe(200)

    environment.NODE_ENV = "production"
    process.env.AUTH_COOKIE_SECURE = "false"
    delete process.env.WEB_TRUSTED_LAN_HTTP
    expect(() => proxy(request)).toThrow("AUTH_COOKIE_SECURE=true")
  })
})

it("replaces forged Lite stamps on measured routes and strips them elsewhere", () => {
  environment.NODE_ENV = "development"
  process.env.PERLIMEN_LITE_BFF_LOG = "/unused/bff.jsonl"
  const id = "x-perlimen-lite-request-id"
  const time = "x-perlimen-lite-start-us"
  const forged = "123456789abc4e12a123123456789abc"
  const headers = { [id]: forged, [time]: "1234" }
  const target = proxy(new NextRequest("http://localhost/api/events/private-slug/check-in", { method: "POST", headers }))
  expect(target.headers.get(`x-middleware-request-${id}`)).toMatch(/^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/)
  expect(target.headers.get(`x-middleware-request-${id}`)).not.toBe(forged)
  expect(Number(target.headers.get(`x-middleware-request-${time}`))).toBeGreaterThan(1234)
  expect(target.headers.has(id)).toBe(false)
  expect(target.headers.has(time)).toBe(false)

  const unrelated = proxy(new NextRequest("http://localhost/api/auth/me", { headers }))
  expect(unrelated.headers.has(`x-middleware-request-${id}`)).toBe(false)
  expect(unrelated.headers.has(`x-middleware-request-${time}`)).toBe(false)
})
