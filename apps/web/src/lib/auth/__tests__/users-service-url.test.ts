import { describe, expect, it } from "bun:test"

import {
  resolveUsersServiceUrl,
  UsersServiceConfigurationError,
} from "@/lib/auth/users-service-url"

describe("users service URL config", () => {
  it("requires USERS_SERVICE_URL for product auth", () => {
    expect(() => resolveUsersServiceUrl(undefined)).toThrow(
      UsersServiceConfigurationError,
    )
    expect(() => resolveUsersServiceUrl("")).toThrow(
      UsersServiceConfigurationError,
    )
  })

  it("normalizes a configured users service URL", () => {
    expect(resolveUsersServiceUrl("http://users.example.test/")).toBe(
      "http://users.example.test",
    )
  })

  it("pins Lite to the exact loopback users endpoint on the launcher port", () => {
    const base = "http://127.0.0.1:3800/users"
    expect(resolveUsersServiceUrl(`${base}/`, "3800")).toBe(base)
    for (const other of [
      "http://users.example.test:3800/users",
      "http://127.0.0.1:3801/users",
      "http://127.0.0.1:3800@users.example.test/users",
      "http://127.0.0.1:3800/social",
      `${base}?other=true`,
    ]) {
      expect(() => resolveUsersServiceUrl(other, "3800")).toThrow(UsersServiceConfigurationError)
    }
    expect(() => resolveUsersServiceUrl(base, "")).toThrow(UsersServiceConfigurationError)
  })
})
