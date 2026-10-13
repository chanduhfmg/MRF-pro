import { motion } from "framer-motion";
import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
  className?: string;
  hover?: boolean;
  as?: "div" | "section" | "article";
  onClick?: () => void;
  delay?: number;
  /** native tooltip on hover */
  title?: string;
}

export default function GlassCard({
  children,
  className = "",
  hover = false,
  onClick,
  delay = 0,
  title,
}: Props) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay, ease: [0.22, 1, 0.36, 1] }}
      onClick={onClick}
      title={title}
      className={`glass ${hover ? "glass-hover" : ""} ${onClick ? "cursor-pointer" : ""} ${className}`}
    >
      {children}
    </motion.div>
  );
}
