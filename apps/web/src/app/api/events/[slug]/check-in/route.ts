import { proxyEventsMutation } from "@/lib/events/client"
import { minimalCheckInResponse } from "@/lib/events/access"
import { observeLiteResponse } from "@/lib/http/lite-diagnostics"

export const dynamic = "force-dynamic"

type Context = { params: Promise<{ slug: string }> }

export async function POST(request: Request, { params }: Context) {
  await observeLiteResponse("/api/events/[slug]/check-in", request.headers)
  const { slug } = await params
  return proxyEventsMutation(
    request,
    `/v1/events/${encodeURIComponent(slug)}/check-in`,
    "POST",
    { successBody: minimalCheckInResponse, hideErrorBody: true },
  )
}
