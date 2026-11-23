# Service tickets: manual accessibility checklist

This checklist is for a person who tests the service ticket portal and the ticket management screens by hand. You do not need to read code. It covers what the automated scan (axe, task 25.2) cannot judge: screen reader use, keyboard use, real colour contrast, 200 % text size in real browsers, and touch target size.

Requirements covered: R23.1 to R23.15, R8.9 (layout from 320 px), R12.5 and R17.5 (polling). The target is Israeli standard IS 5568 (WCAG 2.0) level AA. This pass is a self-check. It is not a formal conformance audit.

## 1. Before you start

### 1.1 Preconditions

- [ ] A **test environment** is running the build you want to accept. Do not test on production data.
- [ ] The feature flag is **on**: `SERVICE_TICKETS_ENABLED=true` in the environment of that build. Check: `/api/features` returns `service_tickets: true`, and `/service` shows the sign-in screen (with the flag off it shows the "service unavailable" page instead).
- [ ] `SERVICE_ACCESSIBILITY_CONTACT` is set to a real phone number or email address. Without it the accessibility statement shows a "contact details not set" message and the footer shows no contact.
- [ ] At least one **category** is active (otherwise the ticket form cannot be completed).
- [ ] Test accounts exist:

| Account | Needed for | Notes |
|---|---|---|
| Resident A, state **initial password** (1234) | Forced change-password screen | Create in Management, Customers, "New customer". New customers must change the password at first sign-in. |
| Resident B, password already changed, with **no tickets** | Empty ticket list | |
| Resident C, password already changed, with **at least 3 tickets** in different statuses (received, in progress, closed, cancelled), at least one marked **urgent**, and one with a **photo** and one without | List, detail, cancel dialog | Use a JPEG under 10 MB for the photo. |
| Staff: role `admin` or `operations_admin` | All Management screens | |
| Staff: role `technician` | Not-authorized and "not available" checks | A technician sees no configuration screens. |
| Staff: a role outside admin, operations_admin and technician (for example accounting) | "Not authorized" screen | Optional but required for the not-authorized row. |

- [ ] You can run every item below in **both Hebrew (he) and English (en)**. Do each screen twice, once per language. The portal language switch is in the page header. Management screens follow the console language.
- [ ] Tools ready (see 1.2 and 1.3).

### 1.2 Equipment and assistive technology

Pick **one desktop** and **one mobile** screen reader and use the same pair for the whole pass. Write them in the recording block (section 11).

| Platform | Choose one | Browser to pair with |
|---|---|---|
| Desktop (Windows) | NVDA (free) or JAWS | NVDA: Firefox or Chrome. JAWS: Chrome or Edge. |
| Desktop (Mac) | VoiceOver (Cmd+F5) | Safari |
| Mobile (Android) | TalkBack | Chrome (or Samsung Internet) |
| Mobile (iPhone) | VoiceOver | Safari |

Hebrew speech: install or enable a Hebrew voice for the screen reader, otherwise Hebrew text is read letter by letter or skipped. If no Hebrew voice is available, say so in the notes and still check that the correct text is announced in writing (NVDA speech viewer, VoiceOver caption panel, or TalkBack "display speech output").

### 1.3 Browsers and sizes

Test the whole pass in at least one desktop browser and one phone browser from the support list of R24.1 (current and previous major versions of Safari on iOS, Chrome on Android, Samsung Internet; current Chrome, Edge, Firefox and Safari on desktop). Items marked **[all browsers]** below must be repeated in each browser you list in the recording block.

Viewport widths for the zoom check: **320, 768, 1280 and 1920 CSS pixels**. On desktop use the browser's responsive mode (F12, device toolbar) or resize the window. 320 px is the narrowest width; use a real phone or the responsive mode set to 320 wide.

### 1.4 How to record

Tick the box when the item passes. If an item fails, leave it empty, write the screen, language and step number in the issue tracker, and put the issue link in the recording block (section 11). Do not tick "N/A" without a reason in the notes.

---

## 2. The ten R23.11 portal screens and the other screens in scope

Portal screens are under `/service`. Management screens are under `/tickets`.

| # | Screen | Address | How to reach it |
|---|---|---|---|
| P1 | Sign-in | `/service` (signed out) | Open the address in a private window. |
| P2 | Forced change-password | `/service` (signed in as Resident A) | Sign in as Resident A. |
| P3 | Ticket list with tickets | `/service` | Sign in as Resident C. |
| P3e | Ticket list, empty | `/service` | Sign in as Resident B. |
| P4 | New ticket form | `/service/new` | "New ticket" / "קריאה חדשה". |
| P4e | New ticket form with validation errors | `/service/new` | Press "Send ticket" / "שליחת הקריאה" with the required fields empty. |
| P5 | Submission confirmation | `/service/new/done` | Send a valid ticket. |
| P6 | Ticket detail with photo | `/service/tickets/<number>` | Open a ticket with a photo. |
| P6n | Ticket detail without photo | same | Open a ticket with no photo. |
| P6c | Cancel dialog on the ticket detail | same | On an open ticket press "Cancel ticket" / "ביטול הקריאה". |
| P7 | Accessibility statement | `/service/accessibility` | Footer link. Also open it signed out. |
| P8 | Not found, unavailable | `/service/tickets/99999` (a number that is not yours), `/service/nothing-here`, and `/service` with the flag off | See P8a to P8d in section 4. |
| M1 | Dashboard | `/tickets` | Staff sign-in. |
| M2 | Ticket list | `/tickets/list` | "Tickets". |
| M3 | Ticket detail | `/tickets/list/<number>` | Open a ticket from the list. |
| M4 | Customers | `/tickets/customers` | "Customers". |
| M5 | Reports | `/tickets/reports` | "Reports". |
| M6 | Categories, field labels, form options | `/tickets/settings/categories`, `/labels`, `/options` | "System settings" in the navigation. |
| M7 | Not authorized | any `/tickets/...` address | Sign in as a role outside the three staff roles, or open a Management address while signed in as a resident. |

---

## 3. Common procedures

Sections 4 to 6 refer to these by letter. Perform them on every screen in the order given in the per-screen tables.

### A. Desktop screen reader pass (NVDA, JAWS or VoiceOver)

- [ ] **A1 Title.** On arrival the window title names the screen followed by "Service tickets" (English) or "קריאות שירות" (Hebrew), for example "My tickets — Service tickets".
- [ ] **A2 Focus on heading.** After you move to the screen from another one, the screen reader reads the screen's main heading (`h1`) first.
- [ ] **A3 Landmarks.** The landmark list (NVDA: D key, or the elements list Insert+F7; VoiceOver: rotor) contains a banner (header), a main area and a footer (content info). On the portal there is also a navigation landmark ("Main navigation" / "ניווט ראשי") when signed in. On Management the navigation is "Service ticket navigation" / "ניווט קריאות שירות".
- [ ] **A4 One heading 1.** The headings list (NVDA: H key, or Insert+F7; VoiceOver rotor) shows exactly one level 1 heading, and the other headings follow in order without a jump.
- [ ] **A5 Skip link.** The first Tab press lands on "Skip to main content" / "דילוג לתוכן הראשי". Pressing Enter moves focus to the main area and the next Tab goes into the page content, not the header.
- [ ] **A6 Reading order.** Read the whole page with the arrow keys. The order matches the visual order, right to left in Hebrew and left to right in English.
- [ ] **A7 Names and roles.** Every button, link, input, select, checkbox and table is announced with a meaningful name and role. Nothing is announced only as "button" or "link" with no name, and no raw identifier or English key text appears in Hebrew.
- [ ] **A8 Language of speech.** The reader switches to the right voice for Hebrew and English text (for example the phone and ticket numbers are read as digits left to right).

### B. Mobile screen reader pass (TalkBack or VoiceOver on a phone)

- [ ] **B1** Swipe right through the whole screen. Every element is reached once, in visual order (in Hebrew the order runs right to left).
- [ ] **B2** Each element is announced with a name and role. Buttons, links and form fields can be activated with a double tap.
- [ ] **B3** The heading navigation (TalkBack: reading controls, Headings; VoiceOver: rotor, Headings) finds the main heading.
- [ ] **B4** Content of dialogs is read, and elements behind a dialog are not reachable while it is open.
- [ ] **B5** The on-screen keyboard shows a numeric keypad for the phone number and the password fields (phone number and 4-digit password).
- [ ] **B6** No control is hidden behind the on-screen keyboard or the browser bar when you focus it, and you can scroll to it.

### C. Keyboard-only operation

Unplug or ignore the mouse. Use Tab, Shift+Tab, Enter, Space, arrow keys and Escape.

- [ ] **C1 Reach everything.** Every link, button, input, select, checkbox, the photo control, the language switch and the refresh control can be reached and operated.
- [ ] **C2 Order.** The Tab order follows the visual reading order: right to left in Hebrew and left to right in English. It does not jump around the page.
- [ ] **C3 Visible focus.** The focused element always has a clearly visible outline (about 3 px). No element loses its outline. Take a screenshot of any element where you cannot see where focus is.
- [ ] **C4 No trap.** You can always Tab out of every region, forwards and backwards (except inside an open dialog, see C5).
- [ ] **C5 Dialogs.** Applies to the cancel dialog (portal) and to the customer dialogs and configuration dialogs (management).
  - [ ] Opening the dialog moves focus into it.
  - [ ] Tab and Shift+Tab stay inside the dialog (focus wraps from last to first and back).
  - [ ] Escape closes the dialog (unless the dialog is busy sending a request).
  - [ ] After the dialog closes (by Escape or by a button), focus returns to the control that opened it, or to the screen's main heading after the action has changed the page.
- [ ] **C6 Activation.** Enter activates links and buttons, Space activates buttons and the checkbox, and arrow keys change the value of a select.
- [ ] **C7 Language switch (portal).** Focus the language switch, press Enter or Space. The language changes without reloading, and **focus stays on the language switch** (see item R23.9 in section 8).
- [ ] **C8 No keyboard-only dead end.** Complete the main task of the screen with the keyboard only (see the per-screen tables).

### D. Live-region announcements

These are read without you moving focus. Run them with a screen reader on. The portal has one polite region (`status`) and one assertive region (`alert`) at the page root.

- [ ] **D1 Language change.** Press the language switch. The screen reader says "Language changed to English." / "השפה שונתה לעברית." in the new language.
- [ ] **D2 Confirmation.** After a ticket is sent, the screen reader says "Ticket number <n> has been received." / "קריאה מספר <n> נקלטה במערכת." without you moving focus.
- [ ] **D3 Cancellation.** After a ticket is cancelled, "Ticket number <n> was cancelled." / "קריאה מספר <n> בוטלה." is read.
- [ ] **D4 Server or network error.** Make a request fail (for example switch the test machine offline and submit the form). A message that belongs to no single field ("We could not confirm that the ticket was sent…", the generic error, rate limit, "New tickets cannot be accepted at this time.") is read at once.
- [ ] **D5 Session expired.** Delete the token (sign out in another tab, or wait for the session to expire in the test environment) and trigger a request. The sign-in screen appears and "Your session has expired. Sign in again." / "פג תוקף ההתחברות. היכנסו שוב." is read.
- [ ] **D6 Status change by polling.** Open the ticket list as Resident C. In Management, change the status of one of that resident's tickets. Within about 30 seconds the portal reads "The status of ticket <n> changed: <status>" / "הסטטוס של קריאה <n> עודכן: <סטטוס>". Only the changed ticket is announced. A reload where nothing changed announces nothing.
- [ ] **D7 Polling keeps state (R23.15).** While you type in a form (or have the cancel dialog open), wait for a 30-second reload. Focus, scroll position and typed values stay as they were, and nothing is announced unless a status changed.
- [ ] **D8 Management notices.** Saving a configuration or customer change reads the success text ("The category was saved.", "The customer details were saved." and so on). A failed action reads its error at once.

### E. Text direction and language (RTL / LTR)

- [ ] **E1** In Hebrew the page flows right to left: text is right-aligned, the header and navigation start at the right edge, and the language switch label reads "English". In English the page flows left to right and the switch reads "עברית".
- [ ] **E2** The browser tab language (screen reader announces the right voice) and the page change together when you switch language. The language switch is read in the language it names (the word "English" with an English voice, the word "עברית" with a Hebrew voice).
- [ ] **E3** Phone numbers and ticket numbers always read and display left to right (digits in order, "0501234567", "10042") even inside Hebrew text. The phone input is left-aligned in both languages.
- [ ] **E4** Punctuation and brackets are on the correct side of Hebrew text, for example "(חובה)" after the label, and "Ticket 10042" in English text containing a Hebrew name does not reorder.
- [ ] **E5** Icons and arrows that show direction ("Back", "Previous", "Next", pagination) point the right way for the language.
- [ ] **E6** The focus order follows the reading order in the current language (see C2).

### F. Text zoom to 200 % in a real browser (R23.7)

Do this at each of 320, 768, 1280 and 1920 CSS pixels. [all browsers]

How to set 200 % text size:

- Desktop Chrome, Edge, Firefox: **Settings, Appearance, Font size** to "Very large" or Settings, Fonts, set the default font size to 32 px; or use browser zoom 200 % (Ctrl and plus until 200 %). For the 320 px width, zoom 400 % of a 1280 px window gives the same CSS width, which is also valid for this check.
- Firefox: Settings, General, Fonts, "Zoom text only" (View, Zoom, Zoom text only) and set 200 %.
- Safari on Mac: Safari, Settings, Advanced, "Never use font sizes smaller than 24", or Cmd and plus.
- Android Chrome: Settings, Accessibility, Text scaling to the maximum, or "Force enable zoom".
- iPhone Safari: Settings, Accessibility, Display and Text Size, Larger Text at the highest value, and the "aA" menu in Safari set to 200 %.

At each width:

- [ ] **F1** No horizontal scrolling of the page (apart from a table that sits in its own scrolling area, if the screen has one, and then the table can be reached and scrolled with the keyboard).
- [ ] **F2** No text is cut off, cropped, hidden behind another element, or replaced by "…".
- [ ] **F3** No text overlaps other text, an icon or a button.
- [ ] **F4** Every function still works: you can reach and press every button, open the language switch, use the photo control, tick the Urgent checkbox, operate the refresh control and read every error message.
- [ ] **F5** The header, language switch and footer stay usable and do not cover the content.
- [ ] **F6** Dialogs fit in the window, their buttons can all be reached (the dialog scrolls if needed), and the title is not cut off.

### G. Touch target size, 44 by 44 CSS pixels (R23.8)

Every control must be at least 44 by 44 CSS pixels, except links inside a sentence of running text. Check at 100 % text size at 320 px and at 1280 px.

How to measure: in the browser developer tools, select the element and read its size in the "Computed" or "Layout" panel (width and height of the box, including padding). Alternatively turn on "Show layout shift regions"/ruler overlay, or use the "Measure" tool in the Firefox responsive mode. On a phone, tap each control with a thumb: accidental taps on a neighbour mean the target is too small or too close.

- [ ] **G1** Buttons (including "Refresh", "Previous", "Next", "Send ticket", dialog buttons) are at least 44 by 44.
- [ ] **G2** Text inputs, selects and the textarea are at least 44 px high.
- [ ] **G3** The Urgent checkbox and any other checkbox have a hit area of at least 44 by 44 (the label next to it counts when tapping it toggles the box).
- [ ] **G4** The photo control ("Choose a photo" / "בחירת תמונה") and the remove button ("Remove the photo" / "הסרת התמונה") are at least 44 by 44.
- [ ] **G5** The language switch, the skip link when focused, the "Sign out" button and the footer "Accessibility statement" link are at least 44 by 44 (a footer link on its own line is not running text).
- [ ] **G6** Links in rows of the ticket list ("Open ticket <n>" / "פתיחת קריאה <n>") are at least 44 by 44 (or the whole row is the target).
- [ ] **G7** Management buttons and links (row actions, filters, pagination, navigation items) are at least 44 by 44 on a phone-sized view. The Management requirement for target size is not in R23.8, so a failure here is a note, not a defect. State the finding in the notes.

### H. Colour contrast in real rendering (R23.4)

axe computes contrast from styles and cannot judge text over images, hover and focus states, disabled looks, or custom browser themes. Check by eye and with a tool in both languages.

Tool: browser DevTools colour picker (shows the ratio), or the free "Colour Contrast Analyser" (TPGi), or WebAIM Contrast Checker with the colour values you read.

Required ratios:

| What | Minimum |
|---|---|
| Normal text (below 18 pt, or below 14 pt bold) | 4.5:1 |
| Large text (18 pt or more, or 14 pt bold or more) | 3:1 |
| Border of an input, select, checkbox, button (the part that shows where the control is) | 3:1 against the background |
| Focus indicator (the outline) | 3:1 against the colours next to it |

- [ ] **H1** Body text, headings, labels and hints (for example "10 digits, for example 0501234567") meet 4.5:1.
- [ ] **H2** Placeholder text and hint text, link text in the footer, pagination text meet 4.5:1.
- [ ] **H3** Error messages (red notices and field errors) meet 4.5:1 on their background.
- [ ] **H4** Status labels (Received, In Progress, Closed, Cancelled), the "Urgent" marker and the management "SOS" marker meet 4.5:1 on their badge background.
- [ ] **H5** Buttons: label text on the button colour meets 4.5:1, in the normal, hover, pressed and **disabled-while-sending** state ("Sending…", "Signing in…"). Disabled controls are allowed to be below ratio only if the page still states the state in text.
- [ ] **H6** Borders of inputs, selects, checkboxes, the photo control and button outlines meet 3:1.
- [ ] **H7** The focus outline meets 3:1 against both the element and the page background, on a light surface and on any coloured badge or banner.
- [ ] **H8** Text on the photo area or over any image meets the ratio (the photo itself is not text and is not tested).
- [ ] **H9** The page still looks right in the operating system's high-contrast mode (Windows "Contrast themes") with no text disappearing and focus still visible.

### I. No information by colour alone (R23.5, R23.12)

- [ ] **I1** Ticket status is always written as text (Received / In Progress / Closed / Cancelled, נקלטה במערכת / בטיפול / נסגרה / בוטלה), with or without an icon. Switch the screen to greyscale (Windows: Settings, Accessibility, Colour filters, Greyscale; iPhone: Display & Text Size, Colour Filters; Android: Colour correction, Grayscale) and confirm every status can still be told apart.
- [ ] **I2** An urgent ticket is marked with the text "Urgent" / "דחוף" on the portal and "SOS" in Management, not only a colour or a red row.
- [ ] **I3** Required fields are marked with the visible text "(required)" / "(חובה)", not only an asterisk or colour.
- [ ] **I4** A field with an error shows an error message in text next to it (starting with "Error:" / "שגיאה:" for screen readers), not only a red border.
- [ ] **I5** The selected / current navigation item is distinguishable without colour (underline, weight, or the screen reader says "current page").
- [ ] **I6** Links inside running text are distinguishable from the text around them without colour (underline), or have 3:1 contrast with the text around them and an additional cue on hover and focus.

---

## 4. Portal screens

For each row, run the common procedures named in the "Common" column plus the screen-specific steps. Repeat in he and en. Mark the result for each screen reader in the recording table in section 11 (one line per screen and language).

### P1. Sign-in (`/service`, signed out)

Common: A, B, C, D5, E, F, G, H, I.

- [ ] Page title is "Sign in — Service tickets" / "כניסה — קריאות שירות". The `h1` reads "Sign in to service tickets" / "כניסה לקריאות שירות".
- [ ] Fields "Phone number" and "Password" (מספר טלפון, סיסמה) each have a visible label that the screen reader reads on focus, and the hints ("10 digits, for example 0501234567", "4 digits") are read after the label.
- [ ] Keyboard: Tab goes phone, password, "Sign in" button. Enter in either field submits. The password field shows dots, not characters.
- [ ] Wrong credentials: the message "The phone number or password is incorrect." / "מספר הטלפון או הסיסמה שגויים." is read and shown, and focus goes to a sensible place (the first field or the message), not to the page top.
- [ ] While sending, the button reads "Signing in…" / "מתחבר…" and cannot be activated twice.
- [ ] Rate limit (try 6 wrong passwords): the "Too many failed attempts…" message with the wait time ("Try again in N seconds.") is read at once.
- [ ] The footer link "Accessibility statement" / "הצהרת נגישות" is present on this screen.
- [ ] Language switch keeps what you typed in both fields and keeps focus on the switch (C7).
- [ ] Phone keypad on the phone (B5).

### P2. Forced change-password (sign in as Resident A)

Common: A, B, C, E, F, G, H, I.

- [ ] Title "Choose a new password — Service tickets" / "בחירת סיסמה חדשה — קריאות שירות".
- [ ] Fields "Current password" and "New password", each with a label, and the hint "Exactly 4 digits" is read.
- [ ] Entering a wrong current password reads "The current password is incorrect." and keeps the other typed values.
- [ ] A new password equal to the initial password (1234) is rejected with a field error that the screen reader reads when the field receives focus.
- [ ] On every path (typing the address `/service`, `/service/new`, or a ticket address), you land on this screen first and cannot reach the list until the password is changed.
- [ ] After a successful change you reach the ticket list.

### P3. Ticket list with tickets (Resident C)

Common: A, B, C, D6, D7, E, F, G, H, I.

- [ ] `h1` "My tickets" / "הקריאות שלי". The table is announced as "List of my tickets" / "רשימת הקריאות שלי" with column headers Ticket number, Category, Date opened, Status (מספר קריאה, קטגוריה, תאריך פתיחה, סטטוס). Moving through cells reads the header for each cell.
- [ ] Each row link is announced as "Open ticket <n>" / "פתיחת קריאה <n>". The ticket number is read as digits.
- [ ] Status is text on every row. The urgent ticket shows "Urgent" / "דחוף" as text.
- [ ] The buttons "New ticket" and "Refresh" can be reached and operated with the keyboard and with the screen reader. After "Refresh" the focus stays on the button.
- [ ] Wait 30 seconds without touching anything. Focus and scroll position do not move (D7). Change a status in Management and confirm the announcement (D6).
- [ ] Pagination (if more than one page; add enough tickets or lower the page size in the test environment): the navigation is "List pages" / "עמודי הרשימה", the buttons read "Previous" and "Next", and "Page 1 of 2" is read. After changing page, focus goes to the `h1`.
- [ ] A page number that does not exist in the address shows "That page is not available. Showing page N." and the message is read.

### P3e. Ticket list, empty (Resident B)

Common: A, B, C, E, F, G, H.

- [ ] The message "You have no service tickets yet." / "עדיין אין לכם קריאות שירות." is read, and the action "Open a new ticket" / "פתיחת קריאה חדשה" is reachable by keyboard.
- [ ] No empty table is announced.

### P4. New ticket form (`/service/new`)

Common: A, B, C, D4, E, F, G, H, I.

- [ ] `h1` "New service ticket" / "קריאת שירות חדשה". The note "Fields marked (required) must be filled in." is read.
- [ ] Every field has a visible label read on focus: Settlement, Apartment number, Issue category, Issue description, Photo, Urgent (ישוב, מספר דירה, קטגוריית תקלה, תיאור התקלה, תמונה, דחוף). The labels may have been changed by the business in Management, M6; then they must match the configured text.
- [ ] Required fields say "(required)" / "(חובה)" in the label text, and the screen reader says "required" on focus. The Description field says "(required)" only when the Description is set to be required in the form options (M6), and "(optional)" / "(אופציונלי)" otherwise. Test both settings.
- [ ] The category select opens with the keyboard and lists the active categories in the current language. The first option "Choose a category" / "בחרו קטגוריה" is not a valid choice.
- [ ] Photo control: with the keyboard, Tab to "Choose a photo", press Enter or Space, the file picker opens. On the phone, the picker offers camera and gallery. After choosing, "Photo selected: <file name>" is read, a preview appears with the alternative text "The photo to attach to the ticket" / "התמונה שתצורף לקריאה", and "Remove the photo" removes it and returns focus to a sensible control.
- [ ] The Urgent checkbox can be ticked with Space. Its help text "Select if the problem is urgent." is read.
- [ ] Tab order is Settlement, Apartment, Category, Description, Photo, Urgent, "Send ticket", "Cancel and return to the list" (reading order).
- [ ] Sending: the button reads "Sending…" / "שולח…" and a second activation is not possible while sending.
- [ ] Language switch keeps every typed value, the chosen category, the Urgent tick and the chosen photo, and keeps focus on the switch.
- [ ] Phone keypad and the on-screen keyboard do not hide the focused field (B6).

### P4e. New ticket form with validation errors

Common: A, B, C, D, E, F, H, I.

- [ ] Press "Send ticket" with the required fields empty. **Focus moves to the first failing field** in reading order (R23.13), and the screen reader reads its label, "invalid" and the error message.
- [ ] Every failing field has its own text error under or next to it, and the screen reader reads that message when each of those fields receives focus (the message is linked to the field).
- [ ] The summary "Correct the fields marked in the form." / "יש לתקן את השדות המסומנים בטופס." is read.
- [ ] Errors are not shown by colour alone (I4) and meet contrast (H3).
- [ ] Correcting a field and sending again removes its error. Typed values stay.
- [ ] A photo that is too large or a file that is not an image shows the matching message ("The photo is too large. Choose a photo of up to 10 MB.") and the message is read.

### P5. Submission confirmation (`/service/new/done`)

Common: A, B, C, D2, E, F, G, H.

- [ ] After a successful send, the title is "Ticket sent — Service tickets" / "הקריאה נשלחה — קריאות שירות". The message "Ticket number <n> has been received." / "קריאה מספר <n> נקלטה במערכת." is read **without moving focus** (D2) and is also visible on the screen.
- [ ] The button "Go to my tickets" / "לרשימת הקריאות" is reachable and works.
- [ ] Reloading the browser on the confirmation screen shows the same confirmation again and sends no second ticket. Opening `/service/new/done` directly (typed address or bookmark) shows the neutral "My tickets" page with the text "There is no new ticket to confirm here…", never a ticket number you did not just send.

### P6. Ticket detail, with a photo and without (open from the list)

Common: A, B, C, D3, D6, D7, E, F, G, H, I.

- [ ] `h1` "Ticket <n>" / "קריאה <n>". The back link "Back to the list" / "חזרה לרשימה" and "Refresh" are reachable.
- [ ] The field names and values (Ticket number, Status, Date opened, Settlement, Apartment, Category, Description) are read as pairs. The date is shown as DD/MM/YYYY. If there is no description, "No description was entered." is shown and read.
- [ ] **With photo:** the photo has the text alternative "Photo for ticket <n>, <category name>" / "תמונה לקריאה <n>, <קטגוריה>" (read by the screen reader). While loading, "Loading the photo…" is shown. A failed load shows "Could not load the photo." with a retry button.
- [ ] **Without photo:** no empty image, no broken image icon, and no stray "image" announcement.
- [ ] The status is text, and the urgent marker is text.
- [ ] "Cancel ticket" / "ביטול הקריאה" appears only for a ticket that can still be cancelled. It does not appear for closed or cancelled tickets.

### P6c. Cancel dialog (ticket detail)

Common: A, B, C5, D3, E, F6, G, H.

- [ ] Activating "Cancel ticket" opens a dialog. The screen reader announces a dialog with the title "Cancel ticket" / "ביטול קריאה" and reads the message "Cancel ticket number <n>? A cancelled ticket cannot be reopened…".
- [ ] Focus moves into the dialog. Tab and Shift+Tab stay inside it. Content behind the dialog cannot be reached with the screen reader (B4).
- [ ] Escape closes the dialog and keeps the ticket. Focus returns to the "Cancel ticket" button or to the page heading.
- [ ] "No, keep the ticket" / "לא, להשאיר את הקריאה" closes the dialog and changes nothing.
- [ ] "Yes, cancel the ticket" / "כן, לבטל את הקריאה" shows "Cancelling…" / "מבטל…" and while pending Escape and double activation do nothing harmful. After success the status shows Cancelled / בוטלה and "Ticket number <n> was cancelled." is read (D3). Focus is on the page heading, not lost on the page.
- [ ] A failed cancel keeps the dialog or page usable and reads "Could not cancel the ticket." at once.
- [ ] **Reload survival:** with the dialog open, wait for the 30 s reload (D7). The dialog stays open and focus stays inside it.
- [ ] Target size and contrast of the dialog buttons (G1, H5).

### P7. Accessibility statement (`/service/accessibility`)

Common: A, B, C, E, F, G, H.

- [ ] Opens **without signing in** (try in a private window) in both languages.
- [ ] `h1` "Accessibility statement" / "הצהרת נגישות". Section headings follow in order: standard and conformance level, what the portal provides, known limitations, accessibility requests.
- [ ] The text names **IS 5568 at level AA** (תקן ישראלי 5568, רמת AA).
- [ ] The known limitations are listed.
- [ ] A **phone number or email address** for accessibility requests is shown (this comes from `SERVICE_ACCESSIBILITY_CONTACT`, see section 1.1). It must not say "contact details have not been set yet".
- [ ] The **date of the last update** is shown (format DD/MM/YYYY) and is correct for this release. See note N3 in section 10.
- [ ] The link to the statement is in the footer of every portal screen: sign-in, change-password, list, form, confirmation, detail, not found. Check each one.

### P8. Not found and unavailable

Common: A, B, C, E, F, G, H.

- [ ] **P8a Ticket not found.** Open `/service/tickets/99999` (a number that does not exist or belongs to another resident). `h1` "Ticket not found" / "הקריאה לא נמצאה", with the text and a "Back to my tickets" button. The message must not reveal whether the ticket exists for someone else.
- [ ] **P8b Page not found.** Open `/service/nothing-here`. `h1` "Page not found" / "הדף לא נמצא", with the back button.
- [ ] **P8c Service unavailable (flag off).** In a second test environment, or after turning the flag off, open `/service` and `/service/new`. The page shows the same bilingual text "השירות אינו זמין כרגע / The service is currently unavailable" on every address, is readable by the screen reader, and has no link to any ticket screen.
- [ ] **P8d Could not check availability.** Block `/api/features` (offline or developer tools, Network, block request URL). The message "Could not check whether the service is available. Try again." is read and the retry button works.

---

## 5. Management screens

Management screens are used by staff on a desktop and do not have a mobile screen reader requirement. Still run the desktop screen reader pass (A) and keyboard pass (C) on each, and the sections F, H, I. Use steps B and G on a phone-size view only for notes. Do each screen in both languages.

### M1. Dashboard (`/tickets`)

- [ ] `h1` "Dashboard" / "לוח בקרה". The three figures (Open tickets, In progress tickets, Urgent (SOS) tickets) are read as label then value, and each has a "Show tickets" link whose name includes both the label and the value.
- [ ] Only for admin and operations_admin, and only when the server reports that ticket numbers are running low: the warning "Only N ticket numbers are left…" is shown and is read by the screen reader as a status message. A technician never sees it. If you cannot make the system report it in the test environment, mark this line N/A with that reason.
- [ ] While figures load, "Loading…" is announced. A failed load reads "Could not load the figures." at once.
- [ ] **Polling.** Leave the page for 30 seconds: the figures refresh with no change to focus or scroll (D7). With the tab in the background nothing is requested (check in developer tools, Network). On returning to the tab the figures refresh at once. If the refresh fails, the old figures stay and "These figures are not current" is shown.
- [ ] Each "Show tickets" link goes to the ticket list with the matching filter.

### M2. Ticket list (`/tickets/list`)

- [ ] `h1` "Service tickets" / "קריאות שירות". The filter area is a search landmark named "Filters" / "סינון". Every filter control has a label: search by ticket number, Status, Category, Settlement, Urgent only, From date, To date, "Urgent open tickets first".
- [ ] The result table has headers (Ticket number, Opened, Customer, Settlement, Apartment, Category, Status, Assigned technician, Urgency). The ticket number links read "Open ticket <n>".
- [ ] The total count "Total: N" is read as a status message after search and after filtering.
- [ ] Status is text, and urgent tickets say "SOS" as text, not only a red row (I2).
- [ ] The row action buttons read the action and the ticket, for example "Move to in progress, ticket 10042". Activating one changes the status, and the new status is shown as text. A failed action reads "The action … on ticket … failed." at once.
- [ ] An empty result reads "No tickets match the search and filters." A list error shows "Could not load the ticket list." with a working "Try again" button.
- [ ] Pagination navigation "List pages" and the range text "1–20 of N" are read.
- [ ] Keyboard: you can search, filter, open a ticket, run a row action and change page without a mouse.

### M3. Ticket detail (`/tickets/list/<number>`)

- [ ] `h1` "Ticket <n>" / "קריאה <n>". Section headings (Ticket details, Photo, Change status, Assigned technician, Treatment history) are `h2` and in order.
- [ ] Customer, phone, settlement, apartment, category, description, opened date are read as label and value. The phone and ticket number read left to right.
- [ ] Photo (when present): alternative text "Photo for ticket <n>, <category>", and the "Open the photo at full size" link works with the keyboard.
- [ ] Change status: the allowed status choices are buttons or a select with labels. On moving to "in progress" the optional "Assigned technician name (optional)" field has a label and the hint "If no name is entered, your username is used." A failed change reads the error at once, and a status that changed meanwhile shows the current status.
- [ ] Assigned technician: the field has a label and the hint "Up to 100 characters". Saving reads "The technician name was saved.".
- [ ] Treatment history: the note "The history is an internal working log and is not shown to the customer." is present. Adding and editing an entry works by keyboard. Each entry shows its time and the technician, and the entry form fields have labels.
- [ ] A ticket number that does not exist shows "Ticket not found" with a link back.

### M4. Customers (`/tickets/customers`)

- [ ] `h1` "Customers" / "דיירים". "New customer" button, search field "Search by name or phone" (inside a search landmark), the "Show" filter (all, active only, inactive only) and the table with headers.
- [ ] Row buttons are read with the customer's name, for example "Edit, <name>", "Deactivate, <name>", "Reset password, <name>".
- [ ] State (Active / Inactive) is text.
- [ ] **Dialogs** (new customer, edit, deactivate, reactivate, reset password): run C5. The confirmation text names the customer. After the action, a status message is read ("The customer was added.", "The password was reset to 1234…").
- [ ] Phone field has a label, the hint "10 digits starting with 0…", and an error message linked to the field when invalid ("This phone number is already registered to another customer." for a duplicate).

### M5. Reports (`/tickets/reports`)

- [ ] `h1` "Service ticket reports" / "דוחות קריאות שירות". The filter form (From date, To date, Settlement, Category, "Show report") has labels on every field.
- [ ] Results (Total tickets, Open tickets, By status, By category, the ticket table) are read in a logical order. The table has headers. Ticket links read "Open ticket <n>".
- [ ] A report error reads "Could not load the report." at once and **the filters stay filled in**.
- [ ] An empty result reads "No tickets match the filters.".
- [ ] "Loading…" is announced while the report loads.

### M6. Categories, field labels, form options (`/tickets/settings/...`)

- [ ] Navigation: under "System settings" / "הגדרות המערכת" the entries "Issue categories", "Field labels", "Form options" are present for admin and operations_admin only, and absent for a technician.
- [ ] **Categories** (`/tickets/settings/categories`): table of categories with Hebrew name, English name and state (Active / Inactive) as text. "Add category" opens a dialog (C5) with labeled fields "Name in Hebrew", "Name in English" and the hint "1 to 60 characters". Deactivate, reactivate and delete buttons are read with the category name. The delete dialog names the category. The result text ("The category was saved.", "The category was deactivated, not deleted, because tickets use it.", and so on) is read as a status message. Try to deactivate the last active category: the error "The last active category cannot be deactivated or deleted…" is read at once.
- [ ] **Field labels** (`/tickets/settings/labels`): each field has a label and the two inputs "Label in Hebrew" / "Label in English" with the hint "1 to 60 characters". Saving reads "The labels were saved.". An invalid value shows an error linked to its field.
- [ ] **Form options** (`/tickets/settings/options`): the two checkboxes ("The issue description is required", "Allow a photo on a ticket") have labels and can be toggled with Space. The help text is read. Saving reads "The settings were saved.". After you change "The issue description is required", open the portal form (P4) and check that "(required)" appears or disappears on the Description label.

### M7. Not authorized

- [ ] A staff user with a role outside the three allowed roles (or a signed-in resident who opens a `/tickets` address) sees `h1` "Not authorized" / "אין הרשאה" and the text "You are not authorized to view this screen." The page shows **no ticket data**.
- [ ] The links on this page are reachable with the keyboard and read with meaningful names.
- [ ] A technician who opens `/tickets/settings/categories` sees "Screen not available" / the matching Hebrew text, not the configuration.
- [ ] With the flag off, a technician sees "The service ticket system is unavailable" with only a sign-out button.
- [ ] A signed-out user who opens a `/tickets` address goes to the console sign-in and returns to the address after signing in.

---

## 6. Management and portal: shared checks to repeat on every screen above

For quick reference, the matrix below shows which common procedures apply to which screens. Do each applicable procedure once per screen, language and browser.

| Procedure | Portal screens | Management screens |
|---|---|---|
| A. Desktop screen reader | all | all |
| B. Mobile screen reader | all | notes only |
| C. Keyboard | all | all |
| D. Live regions | as listed in each row | D8 |
| E. RTL / LTR | all | all |
| F. 200 % text, four widths | all | 1280 and 1920 required, 320 and 768 as notes |
| G. 44 px targets | all | notes only (G7) |
| H. Contrast | all | all |
| I. No colour alone | all | all |

---

## 7. Cross-screen portal checks

- [ ] **X1 Footer link everywhere.** Every portal screen (P1 to P8) has the "Accessibility statement" / "הצהרת נגישות" link in the footer.
- [ ] **X2 No link to the console.** No portal screen contains a link to a Management or Hours console screen.
- [ ] **X3 Language is remembered.** Switch language, reload the page: the chosen language stays. With browser storage blocked (private mode, or storage disabled), the portal still works in Hebrew by default and the switch works for the session.
- [ ] **X4 Heading focus on navigation.** After you open a different screen through a link, the screen reader starts at the new screen's `h1` (A2).
- [ ] **X5 Sign out.** "Sign out" / "התנתקות" is reachable and returns to the sign-in screen with focus on a sensible element.
- [ ] **X6 Bookmarked address.** Open `/service/tickets/<n>` signed out. After sign-in (and change of password if needed) you land on that ticket.
- [ ] **X7 Same host.** In the developer tools, Network, confirm that every request goes to the portal's own host (no font, script, style or image from another host).

---

## 8. Requirement trace

| Requirement | Where it is checked |
|---|---|
| R23.1 labels and required marking | P1, P2, P4, P4e (A7), M4 to M6, I3 |
| R23.2 error linked to field | P4e, P1, P2 |
| R23.3 keyboard, no trap, focus visible, reading order | C1 to C8, E6, all screens |
| R23.4 contrast in real rendering | H1 to H9 |
| R23.5 not colour alone | I1 to I6 |
| R23.6 photo text alternatives | P4 (preview), P6, M3 |
| R23.7 200 % text, 320 to 1920 px | F1 to F6 |
| R23.8 44 by 44 px targets | G1 to G7 |
| R23.9 language switch: html lang, focus stays, accessible name | C7, D1, E1, E2 |
| R23.10 accessibility statement on every screen | P7, X1 |
| R23.11 manual checklist on each listed screen, one desktop and one mobile screen reader | sections 4 and 11 |
| R23.12 Management labels, keyboard, status text | M1 to M7 |
| R23.13 focus to the first failing field | P4e |
| R23.14 non-field messages announced | D1 to D5, D8 |
| R23.15 30 s reload keeps focus, scroll, input; announce only changed status | D6, D7, P3, M1 |

---

## 9. Order of work with the automated scan

The automated scan (`npm run test:a11y` from `frontend/`, task 25.2) must pass before this manual pass counts. Run it first. This checklist does not replace it and the scan does not replace this checklist.

---

## 10. Notes

- **N1. The pass is a manual task for the user.** The agent that wrote this file did not run it. Someone with the assistive technology and real devices has to complete sections 4 to 7 and fill in section 11 before acceptance.
- **N2. Formal audit.** A formal IS 5568 audit is outside this spec (R23). A pass of this checklist supports the statement on the accessibility page but does not certify conformance.
- **N3. Last-updated date.** The accessibility statement shows a fixed date from the locale file (`a11y.statement.lastUpdatedDate`, the same text in both languages, currently `22/11/2026`). On the day of acceptance confirm that this date is the true date of the last change to the statement. If it is not, correct it in both `service.json` locale files and re-test P7.
- **N4. Contact details.** The contact on the statement comes from the setting `SERVICE_ACCESSIBILITY_CONTACT`. The business owner must supply it before release (R23.10).
- **N5. Hebrew voice.** If a Hebrew speech voice is not available on the test device, Hebrew screen reader results are only partial. Note it in section 11.
- **N6. Management screens.** Management has no target size or mobile screen reader requirement in R23. Items G7 and the Management part of B are informative.
- **N7. Browsers.** Items marked [all browsers] (F) and the visual checks (G, H) can differ between browsers. Record the browser for each line.
- **N8. When a step cannot be done.** If a step cannot be done (for example no ticket can be created because no category is active), fix the precondition and repeat. Do not mark it passed.

---

## 11. Recording block

Fill in one block per tester session. Copy the block for each combination of assistive technology and browser.

### 11.1 Session

| Field | Value |
|---|---|
| Tester name | |
| Date (DD/MM/YYYY) | |
| Build or version tested (commit or release) | |
| Environment address | |
| Flag `SERVICE_TICKETS_ENABLED` | on / off |
| Operating system and version | |
| Browser and version | |
| Desktop screen reader and version (NVDA / JAWS / VoiceOver) | |
| Mobile screen reader and version (TalkBack / VoiceOver) | |
| Phone model and OS version | |
| Hebrew voice available | yes / no |
| Languages tested | he / en / both |
| Widths tested for 200 % text | 320 / 768 / 1280 / 1920 |
| Overall result | pass / fail |

### 11.2 Result per screen and language

Write **P** (pass), **F** (fail) or **N/A** with a reason in the notes column. Put every failed step in the issue tracker and write the link in the last column.

| Screen | Lang | Desktop SR (A) | Mobile SR (B) | Keyboard (C) | Live regions (D) | RTL/LTR (E) | 200 % text 320 / 768 / 1280 / 1920 (F) | 44 px (G) | Contrast (H) | Not by colour (I) | Notes | Issue links |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1 Sign-in | he | | | | | | | | | | | |
| P1 Sign-in | en | | | | | | | | | | | |
| P2 Change password | he | | | | | | | | | | | |
| P2 Change password | en | | | | | | | | | | | |
| P3 List with tickets | he | | | | | | | | | | | |
| P3 List with tickets | en | | | | | | | | | | | |
| P3e List, empty | he | | | | | | | | | | | |
| P3e List, empty | en | | | | | | | | | | | |
| P4 New ticket form | he | | | | | | | | | | | |
| P4 New ticket form | en | | | | | | | | | | | |
| P4e Form with errors | he | | | | | | | | | | | |
| P4e Form with errors | en | | | | | | | | | | | |
| P5 Confirmation | he | | | | | | | | | | | |
| P5 Confirmation | en | | | | | | | | | | | |
| P6 Detail with photo | he | | | | | | | | | | | |
| P6 Detail with photo | en | | | | | | | | | | | |
| P6n Detail without photo | he | | | | | | | | | | | |
| P6n Detail without photo | en | | | | | | | | | | | |
| P6c Cancel dialog | he | | | | | | | | | | | |
| P6c Cancel dialog | en | | | | | | | | | | | |
| P7 Accessibility statement | he | | | | | | | | | | | |
| P7 Accessibility statement | en | | | | | | | | | | | |
| P8 Not found / unavailable | he | | | | | | | | | | | |
| P8 Not found / unavailable | en | | | | | | | | | | | |
| M1 Dashboard | he | | n/a | | | | | n/a | | | | |
| M1 Dashboard | en | | n/a | | | | | n/a | | | | |
| M2 Ticket list | he | | n/a | | | | | n/a | | | | |
| M2 Ticket list | en | | n/a | | | | | n/a | | | | |
| M3 Ticket detail | he | | n/a | | | | | n/a | | | | |
| M3 Ticket detail | en | | n/a | | | | | n/a | | | | |
| M4 Customers | he | | n/a | | | | | n/a | | | | |
| M4 Customers | en | | n/a | | | | | n/a | | | | |
| M5 Reports | he | | n/a | | | | | n/a | | | | |
| M5 Reports | en | | n/a | | | | | n/a | | | | |
| M6 Categories | he | | n/a | | | | | n/a | | | | |
| M6 Categories | en | | n/a | | | | | n/a | | | | |
| M6 Field labels | he | | n/a | | | | | n/a | | | | |
| M6 Field labels | en | | n/a | | | | | n/a | | | | |
| M6 Form options | he | | n/a | | | | | n/a | | | | |
| M6 Form options | en | | n/a | | | | | n/a | | | | |
| M7 Not authorized | he | | n/a | | | | | n/a | | | | |
| M7 Not authorized | en | | n/a | | | | | n/a | | | | |

### 11.3 Cross-screen checks

| Item | Result (P / F) | Notes | Issue link |
|---|---|---|---|
| X1 Footer link everywhere | | | |
| X2 No link to the console | | | |
| X3 Language remembered, storage blocked | | | |
| X4 Heading focus on navigation | | | |
| X5 Sign out | | | |
| X6 Bookmarked address | | | |
| X7 Same host only | | | |

### 11.4 Sign-off

| Field | Value |
|---|---|
| Failed items still open | |
| Decision (accept / accept with known limitations / reject) | |
| Known limitations to add to the accessibility statement | |
| Signed by | |
| Date | |
