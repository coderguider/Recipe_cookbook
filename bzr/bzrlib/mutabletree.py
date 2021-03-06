# Copyright (C) 2006-2011 Canonical Ltd
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""MutableTree object.

See MutableTree for more details.
"""

from __future__ import absolute_import

from bzrlib.lazy_import import lazy_import
lazy_import(globals(), """
import operator
import os
import re

from bzrlib import (
    add,
    controldir,
    errors,
    hooks,
    inventory as _mod_inventory,
    osutils,
    revisiontree,
    trace,
    transport as _mod_transport,
    tree,
    )
""")

from bzrlib.decorators import needs_read_lock, needs_write_lock


def needs_tree_write_lock(unbound):
    """Decorate unbound to take out and release a tree_write lock."""
    def tree_write_locked(self, *args, **kwargs):
        self.lock_tree_write()
        try:
            return unbound(self, *args, **kwargs)
        finally:
            self.unlock()
    tree_write_locked.__doc__ = unbound.__doc__
    tree_write_locked.__name__ = unbound.__name__
    return tree_write_locked


class MutableTree(tree.Tree):
    """A MutableTree is a specialisation of Tree which is able to be mutated.

    Generally speaking these mutations are only possible within a lock_write
    context, and will revert if the lock is broken abnormally - but this cannot
    be guaranteed - depending on the exact implementation of the mutable state.

    The most common form of Mutable Tree is WorkingTree, see bzrlib.workingtree.
    For tests we also have MemoryTree which is a MutableTree whose contents are
    entirely in memory.

    For now, we are not treating MutableTree as an interface to provide
    conformance tests for - rather we are testing MemoryTree specifically, and
    interface testing implementations of WorkingTree.

    A mutable tree always has an associated Branch and ControlDir object - the
    branch and bzrdir attributes.
    """
    def __init__(self, *args, **kw):
        super(MutableTree, self).__init__(*args, **kw)
        # Is this tree on a case-insensitive or case-preserving file-system?
        # Sub-classes may initialize to False if they detect they are being
        # used on media which doesn't differentiate the case of names.
        self.case_sensitive = True

    def is_control_filename(self, filename):
        """True if filename is the name of a control file in this tree.

        :param filename: A filename within the tree. This is a relative path
            from the root of this tree.

        This is true IF and ONLY IF the filename is part of the meta data
        that bzr controls in this tree. I.E. a random .bzr directory placed
        on disk will not be a control file for this tree.
        """
        raise NotImplementedError(self.is_control_filename)

    @needs_tree_write_lock
    def add(self, files, ids=None, kinds=None):
        """Add paths to the set of versioned paths.

        Note that the command line normally calls smart_add instead,
        which can automatically recurse.

        This adds the files to the inventory, so that they will be
        recorded by the next commit.

        :param files: List of paths to add, relative to the base of the tree.
        :param ids: If set, use these instead of automatically generated ids.
            Must be the same length as the list of files, but may
            contain None for ids that are to be autogenerated.
        :param kinds: Optional parameter to specify the kinds to be used for
            each file.

        TODO: Perhaps callback with the ids and paths as they're added.
        """
        if isinstance(files, basestring):
            # XXX: Passing a single string is inconsistent and should be
            # deprecated.
            if not (ids is None or isinstance(ids, basestring)):
                raise AssertionError()
            if not (kinds is None or isinstance(kinds, basestring)):
                raise AssertionError()
            files = [files]
            if ids is not None:
                ids = [ids]
            if kinds is not None:
                kinds = [kinds]

        files = [path.strip('/') for path in files]

        if ids is None:
            ids = [None] * len(files)
        else:
            if not (len(ids) == len(files)):
                raise AssertionError()
        if kinds is None:
            kinds = [None] * len(files)
        elif not len(kinds) == len(files):
            raise AssertionError()
        for f in files:
            # generic constraint checks:
            if self.is_control_filename(f):
                raise errors.ForbiddenControlFileError(filename=f)
            fp = osutils.splitpath(f)
        # fill out file kinds for all files [not needed when we stop
        # caring about the instantaneous file kind within a uncommmitted tree
        #
        self._gather_kinds(files, kinds)
        self._add(files, ids, kinds)

    def add_reference(self, sub_tree):
        """Add a TreeReference to the tree, pointing at sub_tree"""
        raise errors.UnsupportedOperation(self.add_reference, self)

    def _add_reference(self, sub_tree):
        """Standard add_reference implementation, for use by subclasses"""
        try:
            sub_tree_path = self.relpath(sub_tree.basedir)
        except errors.PathNotChild:
            raise errors.BadReferenceTarget(self, sub_tree,
                                            'Target not inside tree.')
        sub_tree_id = sub_tree.get_root_id()
        if sub_tree_id == self.get_root_id():
            raise errors.BadReferenceTarget(self, sub_tree,
                                     'Trees have the same root id.')
        if self.has_id(sub_tree_id):
            raise errors.BadReferenceTarget(self, sub_tree,
                                            'Root id already present in tree')
        self._add([sub_tree_path], [sub_tree_id], ['tree-reference'])

    def _add(self, files, ids, kinds):
        """Helper function for add - updates the inventory.

        :param files: sequence of pathnames, relative to the tree root
        :param ids: sequence of suggested ids for the files (may be None)
        :param kinds: sequence of  inventory kinds of the files (i.e. may
            contain "tree-reference")
        """
        raise NotImplementedError(self._add)

    def apply_inventory_delta(self, changes):
        """Apply changes to the inventory as an atomic operation.

        :param changes: An inventory delta to apply to the working tree's
            inventory.
        :return None:
        :seealso Inventory.apply_delta: For details on the changes parameter.
        """
        raise NotImplementedError(self.apply_inventory_delta)

    @needs_write_lock
    def commit(self, message=None, revprops=None, *args,
               **kwargs):
        # avoid circular imports
        from bzrlib import commit
        possible_master_transports=[]
        revprops = commit.Commit.update_revprops(
                revprops,
                self.branch,
                kwargs.pop('authors', None),
                kwargs.pop('author', None),
                kwargs.get('local', False),
                possible_master_transports)
        # args for wt.commit start at message from the Commit.commit method,
        args = (message, ) + args
        for hook in MutableTree.hooks['start_commit']:
            hook(self)
        committed_id = commit.Commit().commit(working_tree=self,
            revprops=revprops,
            possible_master_transports=possible_master_transports,
            *args, **kwargs)
        post_hook_params = PostCommitHookParams(self)
        for hook in MutableTree.hooks['post_commit']:
            hook(post_hook_params)
        return committed_id

    def _gather_kinds(self, files, kinds):
        """Helper function for add - sets the entries of kinds."""
        raise NotImplementedError(self._gather_kinds)

    @needs_read_lock
    def has_changes(self, _from_tree=None):
        """Quickly check that the tree contains at least one commitable change.

        :param _from_tree: tree to compare against to find changes (default to
            the basis tree and is intended to be used by tests).

        :return: True if a change is found. False otherwise
        """
        # Check pending merges
        if len(self.get_parent_ids()) > 1:
            return True
        if _from_tree is None:
            _from_tree = self.basis_tree()
        changes = self.iter_changes(_from_tree)
        try:
            change = changes.next()
            # Exclude root (talk about black magic... --vila 20090629)
            if change[4] == (None, None):
                change = changes.next()
            return True
        except StopIteration:
            # No changes
            return False

    @needs_read_lock
    def check_changed_or_out_of_date(self, strict, opt_name,
                                     more_error, more_warning):
        """Check the tree for uncommitted changes and branch synchronization.

        If strict is None and not set in the config files, a warning is issued.
        If strict is True, an error is raised.
        If strict is False, no checks are done and no warning is issued.

        :param strict: True, False or None, searched in branch config if None.

        :param opt_name: strict option name to search in config file.

        :param more_error: Details about how to avoid the check.

        :param more_warning: Details about what is happening.
        """
        if strict is None:
            strict = self.branch.get_config_stack().get(opt_name)
        if strict is not False:
            err_class = None
            if (self.has_changes()):
                err_class = errors.UncommittedChanges
            elif self.last_revision() != self.branch.last_revision():
                # The tree has lost sync with its branch, there is little
                # chance that the user is aware of it but he can still force
                # the action with --no-strict
                err_class = errors.OutOfDateTree
            if err_class is not None:
                if strict is None:
                    err = err_class(self, more=more_warning)
                    # We don't want to interrupt the user if he expressed no
                    # preference about strict.
                    trace.warning('%s', err._format())
                else:
                    err = err_class(self, more=more_error)
                    raise err

    @needs_read_lock
    def last_revision(self):
        """Return the revision id of the last commit performed in this tree.

        In early tree formats the result of last_revision is the same as the
        branch last_revision, but that is no longer the case for modern tree
        formats.

        last_revision returns the left most parent id, or None if there are no
        parents.

        last_revision was deprecated as of 0.11. Please use get_parent_ids
        instead.
        """
        raise NotImplementedError(self.last_revision)

    def lock_tree_write(self):
        """Lock the working tree for write, and the branch for read.

        This is useful for operations which only need to mutate the working
        tree. Taking out branch write locks is a relatively expensive process
        and may fail if the branch is on read only media. So branch write locks
        should only be taken out when we are modifying branch data - such as in
        operations like commit, pull, uncommit and update.
        """
        raise NotImplementedError(self.lock_tree_write)

    def lock_write(self):
        """Lock the tree and its branch. This allows mutating calls to be made.

        Some mutating methods will take out implicit write locks, but in
        general you should always obtain a write lock before calling mutating
        methods on a tree.
        """
        raise NotImplementedError(self.lock_write)

    @needs_write_lock
    def mkdir(self, path, file_id=None):
        """Create a directory in the tree. if file_id is None, one is assigned.

        :param path: A unicode file path.
        :param file_id: An optional file-id.
        :return: the file id of the new directory.
        """
        raise NotImplementedError(self.mkdir)

    def _observed_sha1(self, file_id, path, (sha1, stat_value)):
        """Tell the tree we have observed a paths sha1.

        The intent of this function is to allow trees that have a hashcache to
        update the hashcache during commit. If the observed file is too new
        (based on the stat_value) to be safely hash-cached the tree will ignore
        it.

        The default implementation does nothing.

        :param file_id: The file id
        :param path: The file path
        :param sha1: The sha 1 that was observed.
        :param stat_value: A stat result for the file the sha1 was read from.
        :return: None
        """

    @needs_write_lock
    def put_file_bytes_non_atomic(self, file_id, bytes):
        """Update the content of a file in the tree.

        Note that the file is written in-place rather than being
        written to a temporary location and renamed. As a consequence,
        readers can potentially see the file half-written.

        :param file_id: file-id of the file
        :param bytes: the new file contents
        """
        raise NotImplementedError(self.put_file_bytes_non_atomic)

    def set_parent_ids(self, revision_ids, allow_leftmost_as_ghost=False):
        """Set the parents ids of the working tree.

        :param revision_ids: A list of revision_ids.
        """
        raise NotImplementedError(self.set_parent_ids)

    def set_parent_trees(self, parents_list, allow_leftmost_as_ghost=False):
        """Set the parents of the working tree.

        :param parents_list: A list of (revision_id, tree) tuples.
            If tree is None, then that element is treated as an unreachable
            parent tree - i.e. a ghost.
        """
        raise NotImplementedError(self.set_parent_trees)

    def smart_add(self, file_list, recurse=True, action=None, save=True):
        """Version file_list, optionally recursing into directories.

        This is designed more towards DWIM for humans than API clarity.
        For the specific behaviour see the help for cmd_add().

        :param file_list: List of zero or more paths.  *NB: these are 
            interpreted relative to the process cwd, not relative to the 
            tree.*  (Add and most other tree methods use tree-relative
            paths.)
        :param action: A reporter to be called with the inventory, parent_ie,
            path and kind of the path being added. It may return a file_id if
            a specific one should be used.
        :param save: Save the inventory after completing the adds. If False
            this provides dry-run functionality by doing the add and not saving
            the inventory.
        :return: A tuple - files_added, ignored_files. files_added is the count
            of added files, and ignored_files is a dict mapping files that were
            ignored to the rule that caused them to be ignored.
        """
        raise NotImplementedError(self.smart_add)


class MutableInventoryTree(MutableTree, tree.InventoryTree):

    @needs_tree_write_lock
    def apply_inventory_delta(self, changes):
        """Apply changes to the inventory as an atomic operation.

        :param changes: An inventory delta to apply to the working tree's
            inventory.
        :return None:
        :seealso Inventory.apply_delta: For details on the changes parameter.
        """
        self.flush()
        inv = self.root_inventory
        inv.apply_delta(changes)
        self._write_inventory(inv)

    def _fix_case_of_inventory_path(self, path):
        """If our tree isn't case sensitive, return the canonical path"""
        if not self.case_sensitive:
            path = self.get_canonical_inventory_path(path)
        return path

    @needs_tree_write_lock
    def smart_add(self, file_list, recurse=True, action=None, save=True):
        """Version file_list, optionally recursing into directories.

        This is designed more towards DWIM for humans than API clarity.
        For the specific behaviour see the help for cmd_add().

        :param file_list: List of zero or more paths.  *NB: these are 
            interpreted relative to the process cwd, not relative to the 
            tree.*  (Add and most other tree methods use tree-relative
            paths.)
        :param action: A reporter to be called with the inventory, parent_ie,
            path and kind of the path being added. It may return a file_id if
            a specific one should be used.
        :param save: Save the inventory after completing the adds. If False
            this provides dry-run functionality by doing the add and not saving
            the inventory.
        :return: A tuple - files_added, ignored_files. files_added is the count
            of added files, and ignored_files is a dict mapping files that were
            ignored to the rule that caused them to be ignored.
        """
        # Not all mutable trees can have conflicts
        if getattr(self, 'conflicts', None) is not None:
            # Collect all related files without checking whether they exist or
            # are versioned. It's cheaper to do that once for all conflicts
            # than trying to find the relevant conflict for each added file.
            conflicts_related = set()
            for c in self.conflicts():
                conflicts_related.update(c.associated_filenames())
        else:
            conflicts_related = None
        adder = _SmartAddHelper(self, action, conflicts_related)
        adder.add(file_list, recurse=recurse)
        if save:
            invdelta = adder.get_inventory_delta()
            self.apply_inventory_delta(invdelta)
        return adder.added, adder.ignored

    def update_basis_by_delta(self, new_revid, delta):
        """Update the parents of this tree after a commit.

        This gives the tree one parent, with revision id new_revid. The
        inventory delta is applied to the current basis tree to generate the
        inventory for the parent new_revid, and all other parent trees are
        discarded.

        All the changes in the delta should be changes synchronising the basis
        tree with some or all of the working tree, with a change to a directory
        requiring that its contents have been recursively included. That is,
        this is not a general purpose tree modification routine, but a helper
        for commit which is not required to handle situations that do not arise
        outside of commit.

        See the inventory developers documentation for the theory behind
        inventory deltas.

        :param new_revid: The new revision id for the trees parent.
        :param delta: An inventory delta (see apply_inventory_delta) describing
            the changes from the current left most parent revision to new_revid.
        """
        # if the tree is updated by a pull to the branch, as happens in
        # WorkingTree2, when there was no separation between branch and tree,
        # then just clear merges, efficiency is not a concern for now as this
        # is legacy environments only, and they are slow regardless.
        if self.last_revision() == new_revid:
            self.set_parent_ids([new_revid])
            return
        # generic implementation based on Inventory manipulation. See
        # WorkingTree classes for optimised versions for specific format trees.
        basis = self.basis_tree()
        basis.lock_read()
        # TODO: Consider re-evaluating the need for this with CHKInventory
        # we don't strictly need to mutate an inventory for this
        # it only makes sense when apply_delta is cheaper than get_inventory()
        inventory = _mod_inventory.mutable_inventory_from_tree(basis)
        basis.unlock()
        inventory.apply_delta(delta)
        rev_tree = revisiontree.InventoryRevisionTree(self.branch.repository,
                                             inventory, new_revid)
        self.set_parent_trees([(new_revid, rev_tree)])


class MutableTreeHooks(hooks.Hooks):
    """A dictionary mapping a hook name to a list of callables for mutabletree
    hooks.
    """

    def __init__(self):
        """Create the default hooks.

        """
        hooks.Hooks.__init__(self, "bzrlib.mutabletree", "MutableTree.hooks")
        self.add_hook('start_commit',
            "Called before a commit is performed on a tree. The start commit "
            "hook is able to change the tree before the commit takes place. "
            "start_commit is called with the bzrlib.mutabletree.MutableTree "
            "that the commit is being performed on.", (1, 4))
        self.add_hook('post_commit',
            "Called after a commit is performed on a tree. The hook is "
            "called with a bzrlib.mutabletree.PostCommitHookParams object. "
            "The mutable tree the commit was performed on is available via "
            "the mutable_tree attribute of that object.", (2, 0))
        self.add_hook('pre_transform',
            "Called before a tree transform on this tree. The hook is called "
            "with the tree that is being transformed and the transform.",
            (2, 5))
        self.add_hook('post_build_tree',
            "Called after a completely new tree is built. The hook is "
            "called with the tree as its only argument.", (2, 5))
        self.add_hook('post_transform',
            "Called after a tree transform has been performed on a tree. "
            "The hook is called with the tree that is being transformed and "
            "the transform.",
            (2, 5))

# install the default hooks into the MutableTree class.
MutableTree.hooks = MutableTreeHooks()


class PostCommitHookParams(object):
    """Parameters for the post_commit hook.

    To access the parameters, use the following attributes:

    * mutable_tree - the MutableTree object
    """

    def __init__(self, mutable_tree):
        """Create the parameters for the post_commit hook."""
        self.mutable_tree = mutable_tree


class _SmartAddHelper(object):
    """Helper for MutableTree.smart_add."""

    def get_inventory_delta(self):
        return self._invdelta.values()

    def _get_ie(self, inv_path):
        """Retrieve the most up to date inventory entry for a path.

        :param inv_path: Normalized inventory path
        :return: Inventory entry (with possibly invalid .children for
            directories)
        """
        entry = self._invdelta.get(inv_path)
        if entry is not None:
            return entry[3]
        # Find a 'best fit' match if the filesystem is case-insensitive
        inv_path = self.tree._fix_case_of_inventory_path(inv_path)
        file_id = self.tree.path2id(inv_path)
        if file_id is not None:
            return self.tree.iter_entries_by_dir([file_id]).next()[1]
        return None

    def _convert_to_directory(self, this_ie, inv_path):
        """Convert an entry to a directory.

        :param this_ie: Inventory entry
        :param inv_path: Normalized path for the inventory entry
        :return: The new inventory entry
        """
        # Same as in _add_one below, if the inventory doesn't
        # think this is a directory, update the inventory
        this_ie = _mod_inventory.InventoryDirectory(
            this_ie.file_id, this_ie.name, this_ie.parent_id)
        self._invdelta[inv_path] = (inv_path, inv_path, this_ie.file_id,
            this_ie)
        return this_ie

    def _add_one_and_parent(self, parent_ie, path, kind, inv_path):
        """Add a new entry to the inventory and automatically add unversioned parents.

        :param parent_ie: Parent inventory entry if known, or None.  If
            None, the parent is looked up by name and used if present, otherwise it
            is recursively added.
        :param path: 
        :param kind: Kind of new entry (file, directory, etc)
        :param inv_path:
        :return: Inventory entry for path and a list of paths which have been added.
        """
        # Nothing to do if path is already versioned.
        # This is safe from infinite recursion because the tree root is
        # always versioned.
        inv_dirname = osutils.dirname(inv_path)
        dirname, basename = osutils.split(path)
        if parent_ie is None:
            # slower but does not need parent_ie
            this_ie = self._get_ie(inv_path)
            if this_ie is not None:
                return this_ie
            # its really not there : add the parent
            # note that the dirname use leads to some extra str copying etc but as
            # there are a limited number of dirs we can be nested under, it should
            # generally find it very fast and not recurse after that.
            parent_ie = self._add_one_and_parent(None,
                dirname, 'directory', 
                inv_dirname)
        # if the parent exists, but isn't a directory, we have to do the
        # kind change now -- really the inventory shouldn't pretend to know
        # the kind of wt files, but it does.
        if parent_ie.kind != 'directory':
            # nb: this relies on someone else checking that the path we're using
            # doesn't contain symlinks.
            parent_ie = self._convert_to_directory(parent_ie, inv_dirname)
        file_id = self.action(self.tree, parent_ie, path, kind)
        entry = _mod_inventory.make_entry(kind, basename, parent_ie.file_id,
            file_id=file_id)
        self._invdelta[inv_path] = (None, inv_path, entry.file_id, entry)
        self.added.append(inv_path)
        return entry

    def _gather_dirs_to_add(self, user_dirs):
        # only walk the minimal parents needed: we have user_dirs to override
        # ignores.
        prev_dir = None

        is_inside = osutils.is_inside_or_parent_of_any
        for path, (inv_path, this_ie) in sorted(
                user_dirs.iteritems(), key=operator.itemgetter(0)):
            if (prev_dir is None or not is_inside([prev_dir], path)):
                yield (path, inv_path, this_ie, None)
            prev_dir = path
        
    def __init__(self, tree, action, conflicts_related=None):
        self.tree = tree
        if action is None:
            self.action = add.AddAction()
        else:
            self.action = action
        self._invdelta = {}
        self.added = []
        self.ignored = {}
        if conflicts_related is None:
            self.conflicts_related = frozenset()
        else:
            self.conflicts_related = conflicts_related

    def add(self, file_list, recurse=True):
        from bzrlib.inventory import InventoryEntry
        if not file_list:
            # no paths supplied: add the entire tree.
            # FIXME: this assumes we are running in a working tree subdir :-/
            # -- vila 20100208
            file_list = [u'.']

        # expand any symlinks in the directory part, while leaving the
        # filename alone
        # only expanding if symlinks are supported avoids windows path bugs
        if osutils.has_symlinks():
            file_list = map(osutils.normalizepath, file_list)

        user_dirs = {}
        # validate user file paths and convert all paths to tree
        # relative : it's cheaper to make a tree relative path an abspath
        # than to convert an abspath to tree relative, and it's cheaper to
        # perform the canonicalization in bulk.
        for filepath in osutils.canonical_relpaths(self.tree.basedir, file_list):
            # validate user parameters. Our recursive code avoids adding new
            # files that need such validation
            if self.tree.is_control_filename(filepath):
                raise errors.ForbiddenControlFileError(filename=filepath)

            abspath = self.tree.abspath(filepath)
            kind = osutils.file_kind(abspath)
            # ensure the named path is added, so that ignore rules in the later
            # directory walk dont skip it.
            # we dont have a parent ie known yet.: use the relatively slower
            # inventory probing method
            inv_path, _ = osutils.normalized_filename(filepath)
            this_ie = self._get_ie(inv_path)
            if this_ie is None:
                this_ie = self._add_one_and_parent(None, filepath, kind, inv_path)
            if kind == 'directory':
                # schedule the dir for scanning
                user_dirs[filepath] = (inv_path, this_ie)

        if not recurse:
            # no need to walk any directories at all.
            return

        things_to_add = list(self._gather_dirs_to_add(user_dirs))

        illegalpath_re = re.compile(r'[\r\n]')
        for directory, inv_path, this_ie, parent_ie in things_to_add:
            # directory is tree-relative
            abspath = self.tree.abspath(directory)

            # get the contents of this directory.

            # find the kind of the path being added, and save stat_value
            # for reuse
            stat_value = None
            if this_ie is None:
                stat_value = osutils.file_stat(abspath)
                kind = osutils.file_kind_from_stat_mode(stat_value.st_mode)
            else:
                kind = this_ie.kind
            
            # allow AddAction to skip this file
            if self.action.skip_file(self.tree,  abspath,  kind,  stat_value):
                continue
            if not InventoryEntry.versionable_kind(kind):
                trace.warning("skipping %s (can't add file of kind '%s')",
                              abspath, kind)
                continue
            if illegalpath_re.search(directory):
                trace.warning("skipping %r (contains \\n or \\r)" % abspath)
                continue
            if directory in self.conflicts_related:
                # If the file looks like one generated for a conflict, don't
                # add it.
                trace.warning(
                    'skipping %s (generated to help resolve conflicts)',
                    abspath)
                continue

            if kind == 'directory' and directory != '':
                try:
                    transport = _mod_transport.get_transport_from_path(abspath)
                    controldir.ControlDirFormat.find_format(transport)
                    sub_tree = True
                except errors.NotBranchError:
                    sub_tree = False
                except errors.UnsupportedFormatError:
                    sub_tree = True
            else:
                sub_tree = False

            if this_ie is not None:
                pass
            elif sub_tree:
                # XXX: This is wrong; people *might* reasonably be trying to
                # add subtrees as subtrees.  This should probably only be done
                # in formats which can represent subtrees, and even then
                # perhaps only when the user asked to add subtrees.  At the
                # moment you can add them specially through 'join --reference',
                # which is perhaps reasonable: adding a new reference is a
                # special operation and can have a special behaviour.  mbp
                # 20070306
                trace.warning("skipping nested tree %r", abspath)
            else:
                this_ie = self._add_one_and_parent(parent_ie, directory, kind,
                    inv_path)

            if kind == 'directory' and not sub_tree:
                if this_ie.kind != 'directory':
                    this_ie = self._convert_to_directory(this_ie, inv_path)

                for subf in sorted(os.listdir(abspath)):
                    inv_f, _ = osutils.normalized_filename(subf)
                    # here we could use TreeDirectory rather than
                    # string concatenation.
                    subp = osutils.pathjoin(directory, subf)
                    # TODO: is_control_filename is very slow. Make it faster.
                    # TreeDirectory.is_control_filename could also make this
                    # faster - its impossible for a non root dir to have a
                    # control file.
                    if self.tree.is_control_filename(subp):
                        trace.mutter("skip control directory %r", subp)
                        continue
                    sub_invp = osutils.pathjoin(inv_path, inv_f)
                    entry = self._invdelta.get(sub_invp)
                    if entry is not None:
                        sub_ie = entry[3]
                    else:
                        sub_ie = this_ie.children.get(inv_f)
                    if sub_ie is not None:
                        # recurse into this already versioned subdir.
                        things_to_add.append((subp, sub_invp, sub_ie, this_ie))
                    else:
                        # user selection overrides ignores
                        # ignore while selecting files - if we globbed in the
                        # outer loop we would ignore user files.
                        ignore_glob = self.tree.is_ignored(subp)
                        if ignore_glob is not None:
                            self.ignored.setdefault(ignore_glob, []).append(subp)
                        else:
                            things_to_add.append((subp, sub_invp, None, this_ie))
