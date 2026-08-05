// The capture box (§10). Prints what you typed to stdout, nothing on cancel.
//
//     osascript -l JavaScript scripts/capture-dialog.js
//     RUGER_DIALOG_SELFTEST=1 osascript -l JavaScript scripts/capture-dialog.js
//
// Why this is not `display dialog`: AppleScript's dialog gives a ONE-LINE field
// that scrolls sideways, so a dictated paragraph has nowhere to go and reads as a
// character limit. This is an NSAlert with a real scrollable NSTextView, which is
// a proper box you can dictate three paragraphs into.
//
// Built through the ObjC bridge rather than compiled, so the repo keeps its "no
// build step" property. The selftest env var constructs the whole view hierarchy
// and returns without showing the modal, which is what makes this assertable.

ObjC.import('AppKit');

const WIDTH = 520;
const HEIGHT = 220;

// NSAlert's first button is 1000. Anything else is a cancel.
const FIRST_BUTTON = 1000;
// NSEventModifierFlagCommand. Spelled numerically because the JXA bridge does not
// reliably expose AppKit's enum constants.
const COMMAND_KEY = 1 << 20;

function run() {
  $.NSApplication.sharedApplication;

  const alert = $.NSAlert.alloc.init;
  alert.setMessageText('What needs doing?');
  alert.setInformativeText(
    'Type, or dictate with Wispr Flow. Several tasks in one go is fine.\n' +
    'Command-Return to capture, Escape to cancel.');
  alert.addButtonWithTitle('Capture');
  alert.addButtonWithTitle('Cancel');

  const text = $.NSTextView.alloc.initWithFrame($.NSMakeRect(0, 0, WIDTH, HEIGHT));
  text.setFont($.NSFont.systemFontOfSize(13));
  text.setRichText(false);
  // Dictation and pasted prose both bring smart quotes and dashes. The verbatim
  // check tolerates them, but turning the substitutions off keeps what is stored
  // closer to what was said.
  text.setAutomaticQuoteSubstitutionEnabled(false);
  text.setAutomaticDashSubstitutionEnabled(false);
  text.setAutomaticTextReplacementEnabled(false);
  text.setTextContainerInset($.NSMakeSize(6, 8));

  const scroll = $.NSScrollView.alloc.initWithFrame($.NSMakeRect(0, 0, WIDTH, HEIGHT));
  scroll.setDocumentView(text);
  scroll.setHasVerticalScroller(true);
  scroll.setBorderType($.NSBezelBorder);
  alert.setAccessoryView(scroll);

  // Return has to reach the text view so it inserts a newline, which means the
  // default button cannot keep its plain Return equivalent.
  const capture = alert.buttons.objectAtIndex(0);
  capture.setKeyEquivalent('\r');
  capture.setKeyEquivalentModifierMask(COMMAND_KEY);

  alert.window.setInitialFirstResponder(scroll.documentView);

  if ($.NSProcessInfo.processInfo.environment.objectForKey('RUGER_DIALOG_SELFTEST').js) {
    return 'selftest-ok';
  }

  $.NSApp.activateIgnoringOtherApps(true);
  const answer = alert.runModal;
  if (answer.js !== FIRST_BUTTON) {
    return '';
  }
  return ObjC.unwrap(text.string) || '';
}
