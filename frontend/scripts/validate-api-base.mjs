const apiBase = process.env.VITE_API_BASE_URL?.trim() ?? "";
const allowLocal = (process.env.ALLOW_LOCAL_API_BASE ?? "").trim().toLowerCase() === "true";

if (!apiBase || allowLocal) {
  process.exit(0);
}

const normalized = apiBase.toLowerCase();
const hasLocalHost =
  normalized.includes("localhost")
  || normalized.includes("127.0.0.1")
  || normalized.includes("[::1]")
  || normalized.includes("0.0.0.0");

if (hasLocalHost) {
  console.error(
    [
      "Refusing to build frontend with a local VITE_API_BASE_URL.",
      `Received: ${apiBase}`,
      "Leave VITE_API_BASE_URL empty to use the safe browser fallback (/api),",
      "or set ALLOW_LOCAL_API_BASE=true only for an intentional local-only build.",
    ].join("\n"),
  );
  process.exit(1);
}
