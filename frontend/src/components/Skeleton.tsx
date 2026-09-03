interface Props {
  className?: string;
  rounded?: string;
}

/** Shimmering placeholder block. */
export function Skeleton({ className = "", rounded = "rounded-lg" }: Props) {
  return (
    <div
      className={`relative overflow-hidden bg-white/[0.05] ${rounded} ${className}`}
    >
      <div className="absolute inset-0 sweep" />
    </div>
  );
}

export function SkeletonText({ lines = 3, className = "" }: { lines?: number; className?: string }) {
  return (
    <div className={`space-y-2 ${className}`}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} className={`h-3.5 ${i === lines - 1 ? "w-2/3" : "w-full"}`} />
      ))}
    </div>
  );
}
