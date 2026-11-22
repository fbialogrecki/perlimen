export class UsersServiceConfigurationError extends Error {
  constructor(message = "USERS_SERVICE_URL is not configured") {
    super(message)
    this.name = "UsersServiceConfigurationError"
  }
}

export function resolveUsersServiceUrl(value: string | undefined, litePort?: string): string {
  if (!value?.trim()) {
    throw new UsersServiceConfigurationError()
  }
  const base = value.replace(/\/$/, "")
  if (litePort !== undefined) {
    const port = Number(litePort)
    if (
      !Number.isInteger(port) || port < 1024 || port > 65535 ||
      String(port) !== litePort || base !== `http://127.0.0.1:${port}/users`
    ) {
      throw new UsersServiceConfigurationError("Lite users service must use its configured loopback endpoint")
    }
  }
  return base
}
