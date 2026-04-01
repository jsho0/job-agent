import { NextResponse } from "next/server";
import { createServiceClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const jobId = searchParams.get("job_id");
  if (!jobId) return NextResponse.json(null);
  const supabase = createServiceClient();
  const { data } = await supabase
    .from("conversations")
    .select("*")
    .eq("job_id", jobId)
    .order("created_at", { ascending: false })
    .limit(1)
    .single();
  return NextResponse.json(data ?? null);
}

export async function POST(req: Request) {
  const supabase = createServiceClient();
  const body = await req.json();
  const { data, error } = await supabase
    .from("conversations")
    .insert(body)
    .select()
    .single();
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });
  return NextResponse.json(data);
}
