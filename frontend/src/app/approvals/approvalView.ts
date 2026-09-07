/**
 * The pure shaping behind the approval view (Requirement 15.3).
 *
 * The API returns a flat, stable-sorted page of time entries. The approval screen groups them two
 * ways so a manager can approve at whichever level fits the work in front of them: **by site**, then
 * by employee within it — a manager runs sites, so approving a site's hours is the natural unit — and
 * the same entries **by employee** for a reviewer who works down a roster. Keeping the shaping pure
 * means the two groupings are tested against fixed inputs here rather than through the rendered tree,
 * and the component stays a thin presentation over the result.
 *
 * The order the API returned is preserved: the list arrives ordered by employee, then work date, then
 * check-in, so the first time a site or an employee is seen fixes its position in the grouping and
 * nothing is re-sorted. Each group carries a status tally so the screen can show, at a glance, how
 * many of a group's entries are still Draft or in Review and how many are Approved — which is what a
 * manager needs to decide whether a bulk step forward is worth taking.
 */

import type { TimeEntryListItem, TimeEntryStatus } from '@/api/types';

/** A count of entries in each status, for a group's tally. */
export interface StatusTally {
  draft: number;
  review: number;
  approved: number;
  locked: number;
  total: number;
}

/** The entries in a group, their ids, and the status tally. */
export interface GroupBucket {
  entries: TimeEntryListItem[];
  entryIds: string[];
  tally: StatusTally;
}

/** One employee's entries within a site group. */
export interface SiteEmployeeGroup extends GroupBucket {
  employeeId: string;
  employeeName: string;
  employeeNameEn: string;
  employeeNumber: string | null;
}

/** One site: its employees' entries, and the site-wide tally. */
export interface SiteApprovalGroup extends GroupBucket {
  siteId: string;
  siteName: string;
  employees: SiteEmployeeGroup[];
}

/** One employee: their entries across sites, and the employee-wide tally. */
export interface EmployeeApprovalGroup extends GroupBucket {
  employeeId: string;
  employeeName: string;
  employeeNameEn: string;
  employeeNumber: string | null;
}

const emptyTally = (): StatusTally => ({
  draft: 0,
  review: 0,
  approved: 0,
  locked: 0,
  total: 0,
});

const addToTally = (tally: StatusTally, status: TimeEntryStatus): void => {
  tally[status] += 1;
  tally.total += 1;
};

const addEntry = (bucket: GroupBucket, entry: TimeEntryListItem): void => {
  bucket.entries.push(entry);
  bucket.entryIds.push(entry.id);
  addToTally(bucket.tally, entry.status);
};

/**
 * Group a flat, ordered page of entries by site, then by employee within each site (Requirement
 * 15.3). A site manager approves the hours worked at a site they run, so this is the grouping the
 * approval action is scoped to; the per-employee sub-grouping lets them approve one person's hours at
 * that site without touching the rest.
 */
export const groupBySite = (entries: TimeEntryListItem[]): SiteApprovalGroup[] => {
  const sites = new Map<string, SiteApprovalGroup>();

  for (const entry of entries) {
    let site = sites.get(entry.site_id);
    if (site === undefined) {
      site = {
        siteId: entry.site_id,
        siteName: entry.site_name,
        entries: [],
        entryIds: [],
        tally: emptyTally(),
        employees: [],
      };
      sites.set(entry.site_id, site);
    }
    addEntry(site, entry);

    let employee = site.employees.find((candidate) => candidate.employeeId === entry.employee_id);
    if (employee === undefined) {
      employee = {
        employeeId: entry.employee_id,
        employeeName: entry.employee_name,
        employeeNameEn: entry.employee_name_en,
        employeeNumber: entry.employee_number,
        entries: [],
        entryIds: [],
        tally: emptyTally(),
      };
      site.employees.push(employee);
    }
    addEntry(employee, entry);
  }

  return Array.from(sites.values());
};

/**
 * Group the same flat page by employee, across sites (Requirement 15.3). The reviewer's grouping: a
 * person's hours for the period, wherever they worked them, so an approver can walk a roster.
 */
export const groupByEmployee = (entries: TimeEntryListItem[]): EmployeeApprovalGroup[] => {
  const employees = new Map<string, EmployeeApprovalGroup>();

  for (const entry of entries) {
    let employee = employees.get(entry.employee_id);
    if (employee === undefined) {
      employee = {
        employeeId: entry.employee_id,
        employeeName: entry.employee_name,
        employeeNameEn: entry.employee_name_en,
        employeeNumber: entry.employee_number,
        entries: [],
        entryIds: [],
        tally: emptyTally(),
      };
      employees.set(entry.employee_id, employee);
    }
    addEntry(employee, entry);
  }

  return Array.from(employees.values());
};

/**
 * The ids in a group that can take a single forward step to `target` (Requirement 15.2). Draft can
 * advance to Review, Review to Approved; an entry already at or past the target, or more than one
 * rung below it, is not eligible for a plain forward bulk step. The approval buttons use this to send
 * only the entries a forward move is legal for, so a bulk "approve" on a mixed group advances the
 * Review entries without the server rejecting the whole call for the Draft ones.
 */
const LADDER: TimeEntryStatus[] = ['draft', 'review', 'approved', 'locked'];

export const advanceableIds = (
  entries: TimeEntryListItem[],
  target: TimeEntryStatus,
): string[] => {
  const targetRank = LADDER.indexOf(target);
  return entries
    .filter((entry) => LADDER.indexOf(entry.status) === targetRank - 1)
    .map((entry) => entry.id);
};
