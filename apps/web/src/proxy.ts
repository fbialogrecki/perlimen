import { type NextRequest, NextResponse } from "next/server"

import { resolveCookieSecurity } from "@/lib/auth/cookie-security"
import { stampLiteRequest } from "@/lib/http/lite-diagnostics"
import { buildSecurityHeaders } from "@/lib/http/security-headers"

export function proxy(request: NextRequest) {
  resolveCookieSecurity({
    nodeEnv: process.env.NODE_ENV,
    authCookieSecure: process.env.AUTH_COOKIE_SECURE,
    trustedLanHttp: process.env.WEB_TRUSTED_LAN_HTTP,
  })
  const headers = stampLiteRequest(request, request.nextUrl.pathname)
  const response = NextResponse.next(headers ? { request: { headers } } : undefined)
  const securityHeaders = buildSecurityHeaders({
    nodeEnv: process.env.NODE_ENV,
    trustedLanHttp: process.env.WEB_TRUSTED_LAN_HTTP === "true",
  })
  for (const { key, value } of securityHeaders) response.headers.set(key, value)
  return response
}

export const config = {
  matcher: "/:path*",
}