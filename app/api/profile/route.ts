import { NextRequest, NextResponse } from "next/server";
import { createServiceClient } from "@/lib/supabase-server";

// Single-user app — one fixed profile row
export const PROFILE_ID = "00000000-0000-0000-0000-000000000001";

export async function GET() {
  const db = createServiceClient();
  const { data } = await db.from("profiles").select("*").eq("id", PROFILE_ID).single();
  return NextResponse.json(data ?? { id: PROFILE_ID, resume_text: "", skills: "", preferences: "" });
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const db = createServiceClient();
  const { data, error } = await db
    .from("profiles")
    .upsert({ id: PROFILE_ID, ...body })
    .select()
    .single();
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });
  return NextResponse.json(data);
}
