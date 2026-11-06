"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

const EVERY = 5_000;

/** Keeps the page current, and stops asking while nobody is looking at it. */
export function Refresher({ every = EVERY }: { every?: number }) {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") router.refresh();
    }, every);
    return () => clearInterval(timer);
  }, [router, every]);
  return null;
}
