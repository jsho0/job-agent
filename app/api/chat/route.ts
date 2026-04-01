import { NextRequest } from "next/server";
import { createServiceClient } from "@/lib/supabase-server";
import { buildSystemPrompt, buildInterviewPrepPrompt, anthropic } from "@/lib/claude";
import type { Message } from "@/lib/supabase";

export const runtime = "nodejs";

export async function POST(req: NextRequest) {
  const { messages, jobId, mode } = (await req.json()) as {
    messages: Message[];
    jobId?: string;
    mode?: "general" | "interview_prep";
  };

  const db = createServiceClient();

  // Load user profile (single-user app, fixed ID)
  const { data: profile } = await db
    .from("profiles")
    .select("*")
    .eq("id", "00000000-0000-0000-0000-000000000001")
    .single();

  // Load job if provided
  let job = null;
  if (jobId) {
    const { data } = await db.from("jobs").select("*").eq("id", jobId).single();
    job = data;
  }

  const systemPrompt =
    mode === "interview_prep"
      ? buildInterviewPrepPrompt(profile, job)
      : buildSystemPrompt(profile, job);

  // Stream response
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      const anthropicStream = await anthropic.messages.stream({
        model: "claude-sonnet-4-6",
        max_tokens: 2048,
        system: systemPrompt,
        messages: messages.map((m) => ({ role: m.role, content: m.content })),
      });

      for await (const chunk of anthropicStream) {
        if (
          chunk.type === "content_block_delta" &&
          chunk.delta.type === "text_delta"
        ) {
          controller.enqueue(encoder.encode(chunk.delta.text));
        }
      }

      controller.close();
    },
  });

  return new Response(stream, {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}
