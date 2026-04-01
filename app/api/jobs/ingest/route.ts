import { NextRequest, NextResponse } from "next/server";
import { createServiceClient } from "@/lib/supabase-server";

export async function POST(req: NextRequest) {
  const apiKey = req.headers.get("x-api-key");
  if (apiKey !== process.env.INGEST_API_KEY) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = await req.json();
  const { title, company, url, description, source } = body;

  if (!title || !company) {
    return NextResponse.json({ error: "title and company are required" }, { status: 400 });
  }

  const db = createServiceClient();
  const { data, error } = await db
    .from("jobs")
    .insert({ title, company, url: url ?? null, description: description ?? null, source: source ?? null, status: "new" })
    .select()
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ id: data.id }, { status: 201 });
}
