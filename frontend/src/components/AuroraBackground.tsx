/**
 * Aurora backdrop: three large, heavily-blurred gradient blobs drifting
 * slowly behind the glass. Fixed, pointer-events-none, low opacity — pure
 * atmosphere, never competes with content. GPU-only transforms.
 */
export default function AuroraBackground() {
  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 -z-10 overflow-hidden bg-ink-950"
    >
      {/* base vignette */}
      <div className="absolute inset-0 bg-[radial-gradient(120%_120%_at_50%_-10%,#141a16_0%,#0a0a0a_55%)]" />

      <div className="absolute left-[-10%] top-[-15%] h-[55vw] w-[55vw] rounded-full bg-[#1a5c38] opacity-[0.22] blur-[130px] animate-aurora-1 will-change-transform" />
      <div className="absolute right-[-15%] top-[10%] h-[48vw] w-[48vw] rounded-full bg-[#0f5f5c] opacity-[0.16] blur-[140px] animate-aurora-2 will-change-transform" />
      <div className="absolute bottom-[-25%] left-[25%] h-[50vw] w-[50vw] rounded-full bg-[#16324f] opacity-[0.14] blur-[150px] animate-aurora-3 will-change-transform" />

      {/* fine grain / noise-ish overlay via subtle gradient lines */}
      <div className="absolute inset-0 opacity-[0.015] bg-[repeating-linear-gradient(0deg,#fff_0_1px,transparent_1px_3px)]" />
    </div>
  );
}
