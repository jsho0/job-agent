import { NextResponse } from "next/server";
import { createServiceClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";

export async function PATCH(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const supabase = createServiceClient();
  const body = await req.json();
  const { error } = await supabase.from("conversations").update(body).eq("id", id);
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });
  return NextResponse.json({ ok: true });
}
