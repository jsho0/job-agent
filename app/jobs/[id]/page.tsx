"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { supabase, type Job, type JobStatus, type Message } from "@/lib/supabase";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { ArrowLeft, ExternalLink, Send, Loader2, Sparkles, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { cn } from "@/lib/utils";
import { CompanyAvatar } from "@/components/company-avatar";

const STATUSES: { key: JobStatus; label: string }[] = [
  { key: "new", label: "New" },
  { key: "saved", label: "Saved" },
  { key: "applied", label: "Applied" },
  { key: "interviewing", label: "Interviewing" },
  { key: "offer", label: "Offer" },
  { key: "rejected", label: "Rejected" },
];

export default function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [job, setJob] = useState<Job | null>(null);
  const [notes, setNotes] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [convId, setConvId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchJob();
    loadConversation();
  }, [id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function fetchJob() {
    const { data } = await supabase.from("jobs").select("*").eq("id", id).single();
    if (data) {
      setJob(data);
      setNotes(data.notes ?? "");
    }
  }

  async function loadConversation() {
    const { data } = await supabase
      .from("conversations")
      .select("*")
      .eq("job_id", id)
      .order("created_at", { ascending: false })
      .limit(1)
      .single();
    if (data) {
      setConvId(data.id);
      setMessages(data.messages ?? []);
    }
  }

  async function updateStatus(status: JobStatus) {
    await supabase.from("jobs").update({ status }).eq("id", id);
    setJob((j) => j ? { ...j, status } : j);
  }

  async function saveNotes() {
    await supabase.from("jobs").update({ notes }).eq("id", id);
  }

  async function deleteJob() {
    if (!confirm("Remove this job from your board?")) return;
    await supabase.from("jobs").delete().eq("id", id);
    router.push("/");
  }

  async function sendMessage() {
    if (!input.trim() || streaming) return;
    const userMsg: Message = { role: "user", content: input.trim() };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput("");
    setStreaming(true);
    setMessages([...newMessages, { role: "assistant", content: "" }]);

    let full = "";
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: newMessages, jobId: id }),
      });

      if (!res.ok) {
        setMessages([...newMessages, { role: "assistant", content: "Something went wrong. Please try again." }]);
        return;
      }

      if (!res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        full += chunk;
        setMessages([...newMessages, { role: "assistant", content: full }]);
      }
    } finally {
      setStreaming(false);
    }
    const finalMessages = [...newMessages, { role: "assistant", content: full }];

    if (convId) {
      await supabase.from("conversations").update({ messages: finalMessages }).eq("id", convId);
    } else {
      const { data } = await supabase
        .from("conversations")
        .insert({ job_id: id, messages: finalMessages })
        .select()
        .single();
      if (data) setConvId(data.id);
    }
  }

  if (!job) {
    return (
      <div className="flex items-center justify-center h-48">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-5">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Back to board
        </Link>
        <button
          onClick={deleteJob}
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-red-400 transition-colors cursor-pointer"
        >
          <Trash2 className="h-3.5 w-3.5" />
          Remove job
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Job Info */}
        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-start gap-3">
                  <CompanyAvatar company={job.company} size="md" className="mt-0.5" />
                  <div>
                    <CardTitle className="text-lg leading-tight">{job.title}</CardTitle>
                    <p className="text-muted-foreground mt-0.5 text-sm">{job.company}</p>
                  </div>
                </div>
                <Badge variant={job.status as any} className="shrink-0 mt-0.5">
                  {job.status}
                </Badge>
              </div>
              {job.url && (
                <a
                  href={job.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-sm text-blue-400 hover:text-blue-300 transition-colors mt-1"
                >
                  View posting
                  <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground uppercase tracking-wider font-medium mb-2">
                Status
              </p>
              <div className="flex flex-wrap gap-1.5">
                {STATUSES.map(({ key, label }) => (
                  <button
                    key={key}
                    onClick={() => updateStatus(key)}
                    className={cn(
                      "text-xs px-2.5 py-1 rounded-full border transition-all",
                      job.status === key
                        ? "bg-blue-500/20 border-blue-500/40 text-blue-300"
                        : "border-border text-muted-foreground hover:border-border hover:text-foreground hover:bg-accent"
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <p className="text-xs text-muted-foreground uppercase tracking-wider font-medium">Notes</p>
            </CardHeader>
            <CardContent>
              <Textarea
                rows={4}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                onBlur={saveNotes}
                placeholder="Your notes about this job..."
              />
            </CardContent>
          </Card>

          {job.description && (
            <Card>
              <CardHeader className="pb-2">
                <p className="text-xs text-muted-foreground uppercase tracking-wider font-medium">
                  Job Description
                </p>
              </CardHeader>
              <CardContent>
                <div className="text-sm text-muted-foreground max-h-56 overflow-y-auto whitespace-pre-wrap leading-relaxed">
                  {job.description}
                </div>
              </CardContent>
            </Card>
          )}
        </div>

        {/* Chat */}
        <Card className="flex flex-col h-[620px]">
          <CardHeader className="border-b border-border pb-3">
            <div className="flex items-center gap-2">
              <Sparkles className="h-3.5 w-3.5 text-primary" />
              <CardTitle className="text-sm font-medium">AI Assistant</CardTitle>
            </div>
            <p className="text-xs text-muted-foreground">
              Ask for a cover letter, interview prep, or role analysis
            </p>
          </CardHeader>
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {messages.length === 0 && (
              <div className="flex flex-col gap-1.5 mt-2">
                {[
                  "Draft a tailored cover letter",
                  "What should I research before applying?",
                  "How does my background fit this role?",
                ].map((prompt) => (
                  <button
                    key={prompt}
                    onClick={() => setInput(prompt)}
                    className="text-left text-xs text-muted-foreground border border-border rounded-lg px-3 py-2.5 hover:border-primary/30 hover:text-foreground hover:bg-accent transition-all cursor-pointer"
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={cn("flex gap-2", m.role === "user" ? "justify-end" : "justify-start")}>
                {m.role === "assistant" && (
                  <div className="h-6 w-6 rounded-full bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
                    <Sparkles className="h-3 w-3 text-primary" />
                  </div>
                )}
                <div
                  className={cn(
                    "text-sm px-3.5 py-2.5 rounded-2xl max-w-[80%] whitespace-pre-wrap leading-relaxed",
                    m.role === "user"
                      ? "bg-primary text-white rounded-tr-sm"
                      : "bg-accent text-foreground rounded-tl-sm"
                  )}
                >
                  {m.content || (streaming && i === messages.length - 1 ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                  ) : null)}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
          <div className="p-3 border-t border-border flex gap-2 bg-background/50">
            <Textarea
              className="flex-1 min-h-0 resize-none text-sm"
              rows={2}
              placeholder="Ask about this job..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
            />
            <Button
              size="icon"
              onClick={sendMessage}
              disabled={streaming || !input.trim()}
              className="self-end shrink-0"
            >
              {streaming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            </Button>
          </div>
        </Card>
      </div>
    </div>
  );
}
