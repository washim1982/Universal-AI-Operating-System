import type { NextConfig } from "next";

const development = process.env.NODE_ENV === "development";

// What a page may load. Scripts are the framework's own, which it inlines, so 'unsafe-inline' stays
// until a nonce is worth a dynamic page; nothing here renders content as HTML, and connect-src and
// form-action keep the page talking only to this console.
const policy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${development ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  `connect-src 'self'${development ? " ws:" : ""}`,
  "base-uri 'none'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "form-action 'self'",
].join("; ");

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  allowedDevOrigins: ["127.0.0.1"],
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: policy },
          { key: "X-Content-Type-Options", value: "nosniff" },
          // Not no-referrer: then a form posted before the page has loaded carries Origin: null,
          // and Next refuses it as cross-site. same-origin still tells other sites nothing.
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default config;
