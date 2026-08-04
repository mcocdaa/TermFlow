# Computer Table Alignment and Delete Feedback Design

## Goal

Make the computer-management page visually consistent by aligning the desktop table and moving delete feedback into a bottom-centered toast that does not disturb the page layout.

## Desktop layout

- Use one shared five-column grid definition for `.computer-table-head` and `.computer-table-row`: `repeat(5, minmax(0, 1fr))`.
- Keep the Name column left-aligned because it contains a primary display name and an optional secondary hostname.
- Center the Terminal, Last Online, Registered At, and Actions column headers.
- Center the matching body cells horizontally while retaining vertical centering.
- Center the trash button, including its SVG, inside the Actions cell.
- Preserve the current row padding, gaps, borders, typography, delete behavior, and accessibility labels.

## Responsive boundary

The equal-column rule applies only while the table header is visible. Existing breakpoints continue to hide the header and turn rows into two-column or one-column cards. Responsive cards keep their existing left-aligned label-and-value presentation.

## Delete feedback

- A successful delete shows `已删除` in a fixed, bottom-centered success toast instead of the inline message above the table.
- A failed delete shows its error in the same bottom toast position with an error treatment.
- The toast floats above page content, does not change table geometry, and remains above the mobile navigation and safe-area inset.
- Each delete notice dismisses automatically after three seconds. A newer delete notice replaces the current one and restarts the timer.
- Use `role="status"` for success and `role="alert"` for errors. The component clears its timer when unmounted.
- Existing inline messages for initial list loading failures and the `已添加` flow remain unchanged; this toast is scoped to delete feedback only.

## Implementation boundary

Keep the change local to `ComputersView.vue` and `packages/client-ui/src/styles/app.css`. There is no existing shared toast system, so a page-scoped delete notice avoids an unrelated application-wide notification refactor. The API, confirmation flow, deletion rules, enrollment flow, and table component structure remain unchanged.

## Verification

- Add a CSS contract test that fails under the current unequal grid, right-aligned Actions cell, and missing fixed bottom toast placement.
- Add view tests proving delete success and failure use the bottom notice, use the correct live-region role, and dismiss after three seconds.
- Run the focused client-ui test, the client-ui suite, type checking, and the web production build.
- Rebuild and recreate the existing Compose control-plane while preserving `termflow-data`.
- Use a real browser at desktop width to assert five equal column widths, the specified alignments, and the centered trash button. Trigger a disposable delete trajectory to assert and capture the deployed bottom toast without deleting an existing user computer.
