"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { type Job, type JobStatus, type Message } from "@/lib/supabase";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft,
  ExternalLink,
  Send,
  Loader2,
  Sparkles,
  Trash2,
  GraduationCap,
  Mic,
  MicOff,
  Volume2,
  VolumeX,
} from "lucide-react";
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

  // Interview prep mode
  const [prepMode, setPrepMode] = useState(false);
  const prepModeRef = useRef(false);

  // Voice state (initialized on mount to avoid SSR mismatch)
  const [voiceSupported, setVoiceSupported] = useState(false);
  const [ttsSupported, setTtsSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [ttsEnabled, setTtsEnabled] = useState(false);
  const [ttsPlaying, setTtsPlaying] = useState(false);
  const recognitionRef = useRef<any>(null);

  useEffect(() => {
    setVoiceSupported(
      "SpeechRecognition" in window || "webkitSpeechRecognition" in window
    );
    setTtsSupported("speechSynthesis" in window);
  }, []);

  useEffect(() => {
    fetchJob();
    loadConversation();
  }, [id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function fetchJob() {
    const res = await fetch(`/api/jobs/${id}`);
    if (res.ok) {
      const data: Job = await res.json();
      setJob(data);
      setNotes(data.notes ?? "");
    }
  }

  async function loadConversation() {
    const res = await fetch(`/api/conversations?job_id=${id}`);
    const data = await res.json();
    if (data) {
      setConvId(data.id);
      setMessages(data.messages ?? []);
    }
  }

  async function updateStatus(status: JobStatus) {
    await fetch(`/api/jobs/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    setJob((j) => (j ? { ...j, status } : j));
  }

  async function saveNotes() {
    await fetch(`/api/jobs/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes }),
    });
  }

  async function deleteJob() {
    if (!confirm("Remove this job from your board?")) return;
    await fetch(`/api/jobs/${id}`, { method: "DELETE" });
    router.push("/");
  }

  // ── Voice ────────────────────────────────────────────────────────────────────

  function startListening() {
    if (!voiceSupported || listening) return;
    const SpeechRecognitionAPI =
      (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    const r = new SpeechRecognitionAPI();
    r.continuous = false;
    r.interimResults = false;
    r.lang = "en-US";
    r.onresult = (e: any) => {
      const transcript: string = e.results[0][0].transcript;
      setInput((prev) => (prev ? prev + " " + transcript : transcript));
      setListening(false);
    };
    r.onerror = () => setListening(false);
    r.onnomatch = () => setListening(false);
    r.onend = () => setListening(false);
    recognitionRef.current = r;
    r.start();
    setListening(true);
  }

  function stopListening() {
    recognitionRef.current?.stop();
    setListening(false);
  }

  function speak(text: string) {
    if (!ttsEnabled || !ttsSupported) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.onstart = () => setTtsPlaying(true);
    utterance.onend = () => setTtsPlaying(false);
    utterance.onerror = () => setTtsPlaying(false);
    window.speechSynthesis.speak(utterance);
  }

  function stopSpeaking() {
    window.speechSynthesis?.cancel();
    setTtsPlaying(false);
  }

  // ── Interview prep ────────────────────────────────────────────────────────────

  function enterPrepMode() {
    prepModeRef.current = true;
    setPrepMode(true);
    if (ttsSupported) setTtsEnabled(true);
    sendMessage("Let's begin my interview prep session.", "interview_prep");
  }

  function exitPrepMode() {
    prepModeRef.current = false;
    setPrepMode(false);
    stopSpeaking();
  }

  // ── Chat ─────────────────────────────────────────────────────────────────────

  async function sendMessage(
    override?: string,
    forceMode?: "general" | "interview_prep"
  ) {
    const text = (override ?? input).trim();
    if (!text || streaming) return;
    const mode = forceMode ?? (prepModeRef.current ? "interview_prep" : "general");
    const userMsg: Message = { role: "user", content: text };
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
        body: JSON.stringify({ messages: newMessages, jobId: id, mode }),
      });

      if (!res.ok) {
        setMessages([
          ...newMessages,
          { role: "assistant", content: "Something went wrong. Please try again." },
        ]);
        return;
      }

      if (!res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        full += decoder.decode(value);
        setMessages([...newMessages, { role: "assistant", content: full }]);
      }
    } finally {
      setStreaming(false);
      if (full) speak(full);
    }

    const finalMessages = [...newMessages, { role: "assistant", content: full }];
    if (convId) {
      await fetch(`/api/conversations/${convId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: finalMessages }),
      });
    } else {
      const data = await fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: id, messages: finalMessages }),
      }).then((r) => r.json());
      if (data?.id) setConvId(data.id);
    }
  }

  if (!job) {
    return (
      <div className="flex items-center justify-center h-48">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const isPrepEligible = job.status === "interviewing";

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
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {prepMode ? (
                  <GraduationCap className="h-3.5 w-3.5 text-primary" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5 text-primary" />
                )}
                <CardTitle className="text-sm font-medium">
                  {prepMode ? "Interview Prep" : "AI Assistant"}
                </CardTitle>
              </div>
              <div className="flex items-center gap-1">
                {ttsSupported && (
                  <button
                    onClick={() => {
                      if (ttsPlaying) stopSpeaking();
                      setTtsEnabled((v) => !v);
                    }}
                    className={cn(
                      "h-6 w-6 rounded flex items-center justify-center transition-all",
                      ttsEnabled
                        ? "text-primary"
                        : "text-muted-foreground hover:text-foreground"
                    )}
                    title={ttsEnabled ? "Mute responses" : "Speak responses aloud"}
                  >
                    {ttsEnabled ? (
                      <Volume2 className="h-3.5 w-3.5" />
                    ) : (
                      <VolumeX className="h-3.5 w-3.5" />
                    )}
                  </button>
                )}
                {isPrepEligible && !prepMode && (
                  <button
                    onClick={enterPrepMode}
                    disabled={streaming}
                    className="ml-1 text-xs px-2.5 py-1 rounded-full border border-primary/30 text-primary hover:bg-primary/10 transition-all disabled:opacity-50 cursor-pointer"
                  >
                    🎯 Practice
                  </button>
                )}
                {prepMode && (
                  <button
                    onClick={exitPrepMode}
                    className="ml-1 text-xs px-2.5 py-1 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-all cursor-pointer"
                  >
                    Exit Prep
                  </button>
                )}
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              {prepMode
                ? "Speak or type your answers — I'll reformulate them as STAR responses"
                : "Ask for a cover letter, interview prep, or role analysis"}
            </p>
          </CardHeader>

          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {messages.length === 0 && !prepMode && (
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
                {isPrepEligible && (
                  <button
                    onClick={enterPrepMode}
                    className="text-left text-xs border border-primary/20 bg-primary/5 text-primary rounded-lg px-3 py-2.5 hover:bg-primary/10 transition-all cursor-pointer"
                  >
                    🎯 Start interview practice session
                  </button>
                )}
              </div>
            )}
            {messages.map((m, i) => (
              <div
                key={i}
                className={cn(
                  "flex gap-2",
                  m.role === "user" ? "justify-end" : "justify-start"
                )}
              >
                {m.role === "assistant" && (
                  <div className="h-6 w-6 rounded-full bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
                    {prepMode ? (
                      <GraduationCap className="h-3 w-3 text-primary" />
                    ) : (
                      <Sparkles className="h-3 w-3 text-primary" />
                    )}
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
                  {m.content ||
                    (streaming && i === messages.length - 1 ? (
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
              placeholder={
                prepMode ? "Speak or type your answer..." : "Ask about this job..."
              }
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
            />
            {voiceSupported && (
              <button
                onClick={listening ? stopListening : startListening}
                disabled={streaming}
                className={cn(
                  "self-end shrink-0 h-9 w-9 rounded-lg border flex items-center justify-center transition-all disabled:opacity-40 cursor-pointer",
                  listening
                    ? "bg-red-500/10 border-red-500/40 text-red-400 animate-pulse"
                    : "border-border text-muted-foreground hover:border-primary/30 hover:text-primary"
                )}
                title={listening ? "Stop recording" : "Voice input"}
              >
                {listening ? (
                  <MicOff className="h-3.5 w-3.5" />
                ) : (
                  <Mic className="h-3.5 w-3.5" />
                )}
              </button>
            )}
            <Button
              size="icon"
              onClick={() => sendMessage()}
              disabled={streaming || !input.trim()}
              className="self-end shrink-0"
            >
              {streaming ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
            </Button>
          </div>
        </Card>
      </div>
    </div>
  );
}
