-- Purge the previous term's roster and its data from the portal.
--
-- Run at a term changeover, when one roster comes out and the next goes in.
--
-- WHAT SURVIVES
--   * every admin account (users.is_admin = 1)
--   * every VM an admin still owns and has not destroyed
--
-- WHAT GOES
--   * every non-admin user, and the whitelist that lets them register
--   * every student group and its membership
--   * every destroyed VM record, and every VM owned by a non-admin
--   * every lab assignment
--   * the whole King of the Hill history, which belongs to the outgoing roster
--
-- BACK UP FIRST. mysqldump, and keep the file. This deletes rows that no
-- application path can recreate.
--
-- The order below is the foreign key order. Children die before parents:
-- koth_* reference koth_games, koth_teams, koth_boxes, users and
-- student_groups; vm_records reference users and student_groups;
-- student_group_members reference both.

SET @admins := (SELECT GROUP_CONCAT(id) FROM users WHERE is_admin = 1);
SELECT CONCAT('admins preserved: ', IFNULL(@admins, 'NONE')) AS note;

-- 1. King of the Hill, leaf tables first.
DELETE FROM koth_flag_submissions;
DELETE FROM koth_claim_events;
DELETE FROM koth_score_snapshots;
DELETE FROM koth_flags;
DELETE FROM koth_boxes;
DELETE FROM koth_teams;
-- vm_records.koth_game_id also points at koth_games, so clear it before the
-- games go. Missing this stops the script at exactly this line with
-- "Cannot delete or update a parent row: fk_vm_records_koth_game".
UPDATE vm_records SET koth_game_id = NULL WHERE koth_game_id IS NOT NULL;
DELETE FROM koth_games;

-- 2. Lab assignments.
DELETE FROM student_lab_assignments;

-- 3. VM records. Drop everything already destroyed, and everything a
--    non-admin owns. An admin's live VM stays.
DELETE FROM vm_records WHERE destroyed = 1;
DELETE FROM vm_records
 WHERE created_by_user_id IS NOT NULL
   AND created_by_user_id NOT IN (SELECT id FROM users WHERE is_admin = 1);

-- 4. Student groups. Membership first, then the groups, then anything that
--    still points at a group.
DELETE FROM student_group_members;
UPDATE vm_records SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
DELETE FROM student_groups;

-- 5. The accounts themselves, and the whitelist that admits them.
DELETE FROM users WHERE is_admin = 0;
DELETE FROM student_whitelist;

-- 6. Report the result. Every student count must be zero, and the admin
--    count must match the line printed at the top.
SELECT (SELECT COUNT(*) FROM student_whitelist)        AS whitelist,
       (SELECT COUNT(*) FROM users WHERE is_admin = 0) AS student_users,
       (SELECT COUNT(*) FROM users WHERE is_admin = 1) AS admins,
       (SELECT COUNT(*) FROM student_groups)           AS groups_left,
       (SELECT COUNT(*) FROM student_group_members)    AS memberships,
       (SELECT COUNT(*) FROM student_lab_assignments)  AS assignments,
       (SELECT COUNT(*) FROM vm_records)               AS vm_records_left,
       (SELECT COUNT(*) FROM koth_games)               AS koth_games;
