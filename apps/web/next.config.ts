import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  output: "standalone",
  reactCompiler: true,
  logging: {
    // These incoming URLs contain single-use credentials.
    incomingRequests: { ignore: [/^\/(?:reset-password|verify-email)\/?(?:\?|$)/] },
  },
};

export default withNextIntl(nextConfig);
