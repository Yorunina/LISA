import { useEffect, useRef } from 'react';

export function ErrorNotice({ message }: { message: string }) {
  const notice = useRef<HTMLParagraphElement>(null);
  useEffect(() => { notice.current?.focus(); }, [message]);
  return <p className="error-notice" role="alert" tabIndex={-1} ref={notice}>{message}</p>;
}
