# Computer Table Alignment Design

## Goal

Make the desktop computer-management table visually consistent by giving all five columns equal width and applying the same horizontal alignment to each header and its corresponding row content.

## Desktop layout

- Use one shared five-column grid definition for `.computer-table-head` and `.computer-table-row`: `repeat(5, minmax(0, 1fr))`.
- Keep the Name column left-aligned because it contains a primary display name and an optional secondary hostname.
- Center the Terminal, Last Online, Registered At, and Actions column headers.
- Center the matching body cells horizontally while retaining vertical centering.
- Center the trash button, including its SVG, inside the Actions cell.
- Preserve the current row padding, gaps, borders, typography, delete behavior, and accessibility labels.

## Responsive boundary

The equal-column rule applies only while the table header is visible. Existing breakpoints continue to hide the header and turn rows into two-column or one-column cards. Responsive cards keep their existing left-aligned label-and-value presentation.

## Implementation boundary

This is a CSS-only production change in `packages/client-ui/src/styles/app.css`. The Vue component structure and delete/add behavior remain unchanged.

## Verification

- Add a CSS contract test that fails under the current unequal grid and right-aligned Actions cell.
- Run the focused client-ui test, the client-ui suite, type checking, and the web production build.
- Rebuild and recreate the existing Compose control-plane while preserving `termflow-data`.
- Use a real browser at desktop width to assert five equal column widths, the specified alignments, and the centered trash button, then capture the deployed Computers page.
