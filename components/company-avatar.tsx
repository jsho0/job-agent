"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";

const AVATAR_COLORS = [
  "bg-violet-500",
  "bg-blue-500",
  "bg-emerald-500",
  "bg-amber-500",
  "bg-rose-500",
  "bg-cyan-500",
  "bg-pink-500",
  "bg-teal-500",
];

function avatarColor(company: string): string {
  let hash = 0;
  for (let i = 0; i < company.length; i++) {
    hash = (hash << 5) - hash + company.charCodeAt(i);
    hash |= 0;
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function companyDomain(company: string): string {
  // Best-effort: take first word, lowercase, strip non-alphanumeric, append .com
  // Works for: Salesforce, HubSpot, Stripe, Google, Amazon, Anthropic, etc.
  const first = company.trim().split(/[\s,.(]/)[0].toLowerCase().replace(/[^a-z0-9]/g, "");
  return `${first}.com`;
}

interface CompanyAvatarProps {
  company: string;
  size?: "sm" | "md";
  className?: string;
}

export function CompanyAvatar({ company, size = "sm", className }: CompanyAvatarProps) {
  const [logoFailed, setLogoFailed] = useState(false);
  const domain = companyDomain(company);
  const sizeClass = size === "md" ? "h-10 w-10 rounded-lg text-sm" : "h-6 w-6 rounded-md text-[9px]";

  if (!logoFailed) {
    return (
      <img
        src={`https://logo.clearbit.com/${domain}`}
        alt={company}
        onError={() => setLogoFailed(true)}
        className={cn(sizeClass, "object-contain bg-white p-0.5 shrink-0", className)}
      />
    );
  }

  return (
    <div
      className={cn(
        sizeClass,
        "flex items-center justify-center font-bold text-white shrink-0",
        avatarColor(company),
        className
      )}
    >
      {company.slice(0, 2).toUpperCase()}
    </div>
  );
}
