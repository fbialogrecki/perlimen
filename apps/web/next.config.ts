import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  output: "standalone",
  reactCompiler: true,
  // Next dev persists raw console output for MCP unless it is disabled here.
  ...(process.env.PERLIMEN_LITE_BFF_LOG && { experimental: { mcpServer: false } }),
};

export default withNextIntl(nextConfig);
