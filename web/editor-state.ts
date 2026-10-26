import { useEffect, useRef } from 'react';
import type { RefObject } from 'react';

export function useUnsavedWarning(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
}

export function useSaveShortcut(enabled: boolean, blocked: boolean, save: () => unknown) {
  const latest = useRef(save);
  latest.current = save;
  useEffect(() => {
    if (!enabled) return;
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        if (!blocked) void latest.current();
      }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, [enabled, blocked]);
}

// Native modal dialogs keep keyboard focus inside and make the background inert.
export function useModalDialog(ref: RefObject<HTMLDialogElement | null>, open: boolean, fallback?: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog || !open) return;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) dialog.showModal();
    // React's autoFocus runs while a newly rendered native dialog is still closed.
    // Move the requested initial focus only after showModal makes its controls visible.
    dialog.querySelector<HTMLElement>('[data-modal-autofocus]')?.focus();
    const wrapFocus = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || event.defaultPrevented) return;
      const controls = [...dialog.querySelectorAll<HTMLElement>(
        'a[href], button, input, select, textarea, [tabindex], [contenteditable="true"]',
      )].filter(element => element.tabIndex >= 0 && !element.matches(':disabled')
        && element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden');
      const active = document.activeElement;
      const first = controls[0];
      const last = controls.at(-1);
      // Native modality makes the page inert, but Tab at either boundary can
      // still move focus into browser chrome. Keep the cycle inside the form.
      if (!first || !last) {
        event.preventDefault();
        dialog.focus();
      } else if (event.shiftKey && (active === first || active === dialog || !dialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || active === dialog || !dialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog.addEventListener('keydown', wrapFocus);
    return () => {
      dialog.removeEventListener('keydown', wrapFocus);
      if (dialog.open) dialog.close();
      if (trigger?.isConnected) trigger.focus();
      else fallback?.current?.focus();
    };
  }, [ref, open, fallback]);
}
