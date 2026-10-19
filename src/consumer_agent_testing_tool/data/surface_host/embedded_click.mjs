// An attached cross-process iframe can be visible before Chromium routes its
// pointer events correctly. Verify pointer delivery before any mouse press.
export async function clickEmbedded(locator, timeout) {
  const deadline = performance.now() + timeout;
  const remaining = () => {
    const value = deadline - performance.now();
    if (value <= 0) throw Object.assign(Error('UI pointer readiness timed out before click'), {category: 'interaction'});
    return value;
  };
  while (true) {
    await locator.hover({timeout: remaining()});
    // :scope keeps the check specific to this element in quirks-mode apps too.
    if (await locator.evaluate(element => element.matches(':scope:hover'))) break;
    await new Promise(resolve => setTimeout(resolve, Math.min(25, remaining())));
  }
  // A click may already have committed even if its reply fails. Never retry it.
  await locator.click({timeout: remaining()});
}
