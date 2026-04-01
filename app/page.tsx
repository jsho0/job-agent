"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { type Job, type JobStatus } from "@/lib/supabase";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Plus, X, ExternalLink, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { CompanyAvatar } from "@/components/company-avatar";

const STATUS_COLUMNS: { key: JobStatus; label: string }[] = [
  { key: "new", label: "New" },
  { key: "saved", label: "Saved" },
  { key: "applied", label: "Applied" },
  { key: "interviewing", label: "Interviewing" },
  { key: "offer", label: "Offer" },
  { key: "rejected", label: "Rejected" },
];


function SkeletonCard() {
  return (
    <div className="rounded-lg border border-border bg-card p-3 space-y-2.5 animate-pulse">
      <div className="flex items-center gap-2">
        <div className="h-6 w-6 rounded-md bg-muted shrink-0" />
        <div className="h-3 bg-muted rounded w-2/3" />
      </div>
      <div className="h-2.5 bg-muted rounded w-1/2" />
      <div className="h-4 bg-muted rounded-full w-10 mt-1" />
    </div>
  );
}

function StatPill({ label, value, highlight }: { label: string; value: number | string; highlight?: boolean }) {
  const isHighlighted = highlight && (typeof value === "number" ? value > 0 : true);
  return (
    <div className={cn(
      "flex items-center gap-2 px-4 py-2.5 rounded-lg border",
      isHighlighted ? "bg-primary/5 border-primary/20" : "bg-card border-border"
    )}>
      <span className={cn(
        "text-xl font-bold tabular-nums",
        isHighlighted ? "text-primary" : "text-foreground"
      )}>
        {value}
      </span>
      <span className="text-xs text-muted-foreground leading-tight">{label}</span>
    </div>
  );
}

export default function JobBoard() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [newJob, setNewJob] = useState({ title: "", company: "", url: "", description: "" });
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [fetchingDesc, setFetchingDesc] = useState(false);

  useEffect(() => {
    fetchJobs();
  }, []);

  async function fetchJobs() {
    const data = await fetch("/api/jobs").then((r) => r.json());
    setJobs(Array.isArray(data) ? data : []);
    setLoading(false);
  }

  async function fetchDescription() {
    if (!newJob.url || fetchingDesc) return;
    setFetchingDesc(true);
    try {
      const res = await fetch("/api/jobs/fetch-description", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: newJob.url }),
      });
      const data = await res.json();
      if (data.description) setNewJob((j) => ({ ...j, description: data.description }));
      else setAddError("Couldn't fetch description — paste it manually.");
    } finally {
      setFetchingDesc(false);
    }
  }

  async function deleteJob(id: string) {
    await fetch(`/api/jobs/${id}`, { method: "DELETE" });
    setJobs((prev) => prev.filter((j) => j.id !== id));
  }

  async function addJob() {
    if (!newJob.title || !newJob.company || adding) return;
    setAdding(true);
    setAddError(null);
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(newJob),
    });
    setAdding(false);
    if (!res.ok) {
      setAddError("Failed to add job. Check your database connection.");
      return;
    }
    setNewJob({ title: "", company: "", url: "", description: "" });
    setShowAdd(false);
    fetchJobs();
  }

  const jobsByStatus = (status: JobStatus) => jobs.filter((j) => j.status === status);

  const stats = {
    total: jobs.length,
    applied: jobs.filter((j) => j.status === "applied").length,
    interviewing: jobs.filter((j) => j.status === "interviewing").length,
    offers: jobs.filter((j) => j.status === "offer").length,
    interviewRate: (() => {
      const throughApplied = jobs.filter((j) =>
        ["applied", "interviewing", "offer", "rejected"].includes(j.status)
      );
      const converted = jobs.filter((j) =>
        ["interviewing", "offer"].includes(j.status)
      );
      return throughApplied.length >= 3
        ? Math.round((converted.length / throughApplied.length) * 100)
        : null;
    })(),
    topSource: (() => {
      const bySource: Record<string, { total: number; converted: number }> = {};
      for (const job of jobs) {
        if (!job.source) continue;
        if (!["applied", "interviewing", "offer", "rejected"].includes(job.status)) continue;
        bySource[job.source] ??= { total: 0, converted: 0 };
        bySource[job.source].total++;
        if (["interviewing", "offer"].includes(job.status)) bySource[job.source].converted++;
      }
      const entries = Object.entries(bySource).filter(([, v]) => v.total >= 2);
      if (!entries.length) return null;
      entries.sort((a, b) => b[1].converted / b[1].total - a[1].converted / a[1].total);
      return { source: entries[0][0], converted: entries[0][1].converted };
    })(),
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-semibold">Job Board</h1>
          <p className="text-sm text-muted-foreground mt-0.5">Track your applications across every stage</p>
        </div>
        <Button size="sm" onClick={() => setShowAdd(!showAdd)}>
          <Plus className="h-3.5 w-3.5" />
          Add Job
        </Button>
      </div>

      {/* Stats bar */}
      {!loading && (
        <div className="flex gap-3 mb-5 flex-wrap">
          <StatPill label="Total tracked" value={stats.total} />
          <StatPill label="Applied" value={stats.applied} />
          <StatPill label="Interviewing" value={stats.interviewing} highlight />
          <StatPill label="Offers" value={stats.offers} highlight />
          {stats.interviewRate !== null && (
            <StatPill label="interview rate" value={`${stats.interviewRate}%`} highlight={stats.interviewRate > 0} />
          )}
          {stats.topSource && (
            <StatPill label={`interviews via ${stats.topSource.source}`} value={stats.topSource.converted} highlight />
          )}
        </div>
      )}

      {showAdd && (
        <Card className="mb-6 p-5 max-w-lg border-primary/20">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold">Add Job Manually</h2>
            <button
              onClick={() => setShowAdd(false)}
              className="text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="space-y-2.5">
            <Input
              placeholder="Job title *"
              value={newJob.title}
              onChange={(e) => setNewJob({ ...newJob, title: e.target.value })}
            />
            <Input
              placeholder="Company *"
              value={newJob.company}
              onChange={(e) => setNewJob({ ...newJob, company: e.target.value })}
            />
            <div className="flex gap-2">
              <Input
                placeholder="Job posting URL"
                value={newJob.url}
                onChange={(e) => setNewJob({ ...newJob, url: e.target.value })}
                className="flex-1"
              />
              <Button
                size="sm"
                variant="outline"
                onClick={fetchDescription}
                disabled={!newJob.url || fetchingDesc}
                title="Auto-fetch job description from URL"
              >
                {fetchingDesc ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Fetch"}
              </Button>
            </div>
            <Textarea
              placeholder="Job description — auto-filled after Fetch, or paste manually"
              rows={4}
              value={newJob.description}
              onChange={(e) => setNewJob({ ...newJob, description: e.target.value })}
            />
            {addError && (
              <p className="text-xs text-red-400">{addError}</p>
            )}
            <div className="flex gap-2 pt-1">
              <Button size="sm" onClick={addJob} disabled={adding || !newJob.title || !newJob.company}>
                {adding ? "Adding..." : "Add Job"}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setShowAdd(false)}>
                Cancel
              </Button>
            </div>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {STATUS_COLUMNS.map(({ key, label }) => (
          <div key={key} className="flex flex-col gap-2">
            <div className="flex items-center justify-between px-1">
              <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {label}
              </span>
              <span className="text-xs text-muted-foreground bg-muted rounded-full px-1.5 py-0.5 leading-none tabular-nums">
                {loading ? "–" : jobsByStatus(key).length}
              </span>
            </div>
            <div className="space-y-2 min-h-[80px]">
              {loading ? (
                key === "new" || key === "applied" ? (
                  <>
                    <SkeletonCard />
                    <SkeletonCard />
                  </>
                ) : key === "saved" ? (
                  <SkeletonCard />
                ) : null
              ) : jobsByStatus(key).length === 0 ? (
                <div className="rounded-lg border border-dashed border-border/50 min-h-[60px]" />
              ) : (
                jobsByStatus(key).map((job) => (
                  <div key={job.id} className="relative group">
                    <Link href={`/jobs/${job.id}`}>
                      <Card className="p-3 hover:border-primary/30 hover:bg-accent/40 transition-all cursor-pointer group">
                        <div className="flex items-start gap-2 mb-1.5">
                          <CompanyAvatar company={job.company} />
                          <div className="text-xs font-medium leading-snug group-hover:text-primary transition-colors line-clamp-2 pr-4">
                            {job.title}
                          </div>
                        </div>
                        <div className="text-[11px] text-muted-foreground truncate pl-8">{job.company}</div>
                        <div className="flex items-center justify-between mt-2 pl-8">
                          <Badge variant={key as any} className="text-[10px] px-1.5 py-0">
                            {label}
                          </Badge>
                          {job.url && (
                            <ExternalLink className="h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
                          )}
                        </div>
                      </Card>
                    </Link>
                    <button
                      onClick={(e) => {
                        e.preventDefault();
                        deleteJob(job.id);
                      }}
                      className="absolute top-1.5 right-1.5 h-4 w-4 rounded flex items-center justify-center text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-red-400 hover:bg-red-400/10 transition-all cursor-pointer"
                      title="Remove job"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
