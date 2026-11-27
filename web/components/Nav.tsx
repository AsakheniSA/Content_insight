"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Today" },
  { href: "/insights", label: "Insights" },
  { href: "/reddit", label: "Reddit" },
  { href: "/runs", label: "Runs" },
];

export function Nav({ gated }: { gated: boolean }) {
  const path = usePathname();
  if (path === "/login") return null;
  return (
    <nav className="nav">
      {LINKS.map((link) => {
        const active = link.href === "/" ? path === "/" : path.startsWith(link.href);
        return (
          <Link key={link.href} href={link.href} className={active ? "active" : undefined}>
            {link.label}
          </Link>
        );
      })}
      {gated && (
        <form action="/logout" method="post">
          <button type="submit" className="link-button">Sign out</button>
        </form>
      )}
    </nav>
  );
}
