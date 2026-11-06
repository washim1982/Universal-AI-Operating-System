import { ApiError, exported } from "@/lib/api";

const NAME = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

/** Everything an App has, as a download. It goes from AIOS to the person with their credential. */
export async function GET(_: Request, { params }: { params: Promise<{ app: string }> }) {
  const { app } = await params;
  if (!NAME.test(app)) return new Response("No such App.", { status: 404 });
  try {
    const upstream = await exported(app);
    return new Response(upstream.body, {
      headers: {
        "Content-Type": "application/x-ndjson",
        "Content-Disposition": `attachment; filename="${app}-export.jsonl"`,
        "Cache-Control": "no-store",
      },
    });
  } catch (error) {
    if (error instanceof ApiError) return new Response(error.message, { status: error.status });
    throw error;
  }
}
