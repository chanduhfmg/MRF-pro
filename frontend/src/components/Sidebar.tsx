import { NavLink } from "react-router-dom";
import { motion } from "framer-motion";

const NAV = [
  { to: "/upload", label: "Upload", icon: UploadIcon },
  { to: "/jobs", label: "Jobs", icon: JobsIcon },
  { to: "/dashboard", label: "KPI Dashboard", icon: ChartIcon },
];

export default function Sidebar({ extractionMode }: { extractionMode?: "stub" | "real" }) {
  return (
    <aside className="sticky top-0 flex h-screen w-[248px] shrink-0 flex-col gap-1 border-r border-white/[0.06] bg-black/20 px-4 py-6 backdrop-blur-xl">
      <div className="mb-7 flex items-center gap-2.5 px-2">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-accent-500 to-accent-700 shadow-lg shadow-accent-700/30">
          <LogoMark />
        </div>
        <div className="leading-tight">
          <div className="text-[13px] font-semibold tracking-tightish">MRF Extraction</div>
          <div className="text-[11px] text-faint">Transparency-in-Coverage</div>
        </div>
      </div>

      <nav className="flex flex-col gap-1">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `group relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-colors ${
                isActive
                  ? "text-white"
                  : "text-dim hover:text-white hover:bg-white/[0.04]"
              }`
            }
          >
            {({ isActive }) => (
              <>
                {isActive && (
                  <motion.span
                    layoutId="nav-active"
                    className="absolute inset-0 rounded-xl bg-white/[0.06] ring-1 ring-white/10"
                    transition={{ type: "spring", stiffness: 300, damping: 30 }}
                  />
                )}
                <Icon className="relative z-10 h-4 w-4" />
                <span className="relative z-10 font-medium">{label}</span>
              </>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="mt-auto space-y-3 px-2">
        {extractionMode && (
          <div className="flex items-center gap-2 rounded-lg bg-white/[0.03] px-2.5 py-2 text-[11px] ring-1 ring-white/[0.06]">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                extractionMode === "real" ? "bg-accent-400" : "bg-amber-400"
              }`}
            />
            <span className="text-faint">
              engine:{" "}
              <span className={extractionMode === "real" ? "text-accent-400" : "text-amber-300"}>
                {extractionMode}
              </span>
            </span>
          </div>
        )}
        <p className="text-[11px] leading-relaxed text-faint">
          Automated download, extraction &amp; packaging of payer MRFs.
        </p>
      </div>
    </aside>
  );
}

function LogoMark() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5 text-white" fill="none">
      <path
        d="M4 7l8-4 8 4v10l-8 4-8-4V7z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M12 3v18M4 7l8 4 8-4" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

function UploadIcon({ className = "" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className}>
      <path d="M12 16V4m0 0l-4 4m4-4l4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}
function JobsIcon({ className = "" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className}>
      <rect x="3.5" y="5" width="17" height="14" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M7.5 9.5h9M7.5 13h9M7.5 16h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}
function ChartIcon({ className = "" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className}>
      <path d="M4 20V4M4 20h16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <rect x="7.5" y="12" width="3" height="5" rx="1" fill="currentColor" />
      <rect x="13" y="8" width="3" height="9" rx="1" fill="currentColor" />
      <rect x="18" y="14" width="2.2" height="3" rx="1" fill="currentColor" opacity="0.6" />
    </svg>
  );
}
