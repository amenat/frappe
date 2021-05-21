# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

""" Classes and utility functions related to NestedSet.

	Nested sets are used in Frappe Framework for modelling Tree like DocTypes
	Checking `is tree` checkbox while creating a new DocType that inherits from NestedSet.
"""

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now

class NestedSetRecursionError(frappe.ValidationError): pass
class NestedSetMultipleRootsError(frappe.ValidationError): pass
class NestedSetChildExistsError(frappe.ValidationError): pass
class NestedSetInvalidMergeError(frappe.ValidationError): pass


def update_nsm(doc):
	"""Update Nested Set Model.

		Called by NestedSet.on_update.
		When the document is updated, update parent information.
	"""

	# get fields, data from the DocType
	old_parent_field = 'old_parent'
	parent_field = "parent_" + frappe.scrub(doc.doctype)

	if hasattr(doc,'nsm_parent_field'):
		parent_field = doc.nsm_parent_field
	if hasattr(doc,'nsm_oldparent_field'):
		old_parent_field = doc.nsm_oldparent_field

	parent, old_parent = doc.get(parent_field), doc.get(old_parent_field)

	# has parent changed (?) or parent is None (root)
	if not doc.lft and not doc.rgt:
		update_add_node(doc, parent or '', parent_field)
	elif old_parent != parent:
		update_move_node(doc, parent_field)

	# set old parent
	doc.set(old_parent_field, parent)
	frappe.db.set_value(doc.doctype, doc.name, old_parent_field, parent or '', update_modified=False)

	doc.reload()

def update_add_node(doc, parent, parent_field):
	"""Insert a new node in Nested Set Model.

		:param doc: document to insert
		:param parent: name of parent node
		:param parent_field: Link field specifying parent relation.
		:return right domain value of node #TODO ??
	"""

	current_time = now()

	doctype = doc.doctype
	name = doc.name

	# get the last sibling of the parent
	if parent:
		left, right = frappe.db.sql(f"select lft, rgt from `tab{doctype}` where name=%s", parent)[0]
		validate_loop(doc.doctype, doc.name, left, right)
	else: # root
		right = frappe.db.sql(f"""
			SELECT COALESCE(MAX(rgt), 0) + 1 FROM `tab{doctype}`
			WHERE COALESCE(`{parent_field}`, '') = ''
		""")[0][0]
	right = right or 1

	# update all on the right
	frappe.db.sql(f"update `tab{doctype}` set rgt = rgt+2, modified=%s where rgt >= %s", (current_time, right))
	frappe.db.sql(f"update `tab{doctype}` set lft = lft+2, modified=%s where lft >= %s", (current_time, right))

	# update index of new node
	if frappe.db.sql(f"select * from `tab{doctype}` where lft=%s or rgt=%s", (right, right+1)):
		frappe.msgprint(_("Nested set error. Please contact the Administrator."))
		raise Exception

	frappe.db.sql(f"update `tab{doctype}` set lft=%s, rgt=%s, modified=%s where name=%s", (right,right+1, current_time, name))
	return right


def update_move_node(doc, parent_field):
	"""Called when a document is updated to change it's parent.
	"""
	current_time = now()
	parent = doc.get(parent_field)

	if parent:
		new_parent = frappe.db.sql(f"""select lft, rgt from `tab{doc.doctype}`
			where name = %s""", parent, as_dict=1)[0]

		validate_loop(doc.doctype, doc.name, new_parent.lft, new_parent.rgt)

	# move to dark side
	frappe.db.sql(f"""update `tab{doc.doctype}` set lft = -lft, rgt = -rgt, modified=%s
		where lft >= %s and rgt <= %s""", (current_time, doc.lft, doc.rgt))

	# shift left
	diff = doc.rgt - doc.lft + 1
	frappe.db.sql(f"""update `tab{doc.doctype}` set lft = lft -%s, rgt = rgt - %s, modified=%s
		where lft > %s""", (diff, diff, current_time, doc.rgt))

	# shift left rgts of ancestors whose only rgts must shift
	frappe.db.sql(f"""update `tab{doc.doctype}` set rgt = rgt - %s, modified=%s
		where lft < %s and rgt > %s""", (diff, current_time, doc.lft, doc.rgt))

	if parent:
		new_parent = frappe.db.sql(f"""select lft, rgt from `tab{doc.doctype}`
			where name = %s""", parent, as_dict=1)[0]

		# set parent lft, rgt
		frappe.db.sql(f"""update `tab{doc.doctype}` set rgt = rgt + %s, modified=%s
			where name = %s""", (diff, current_time, parent))

		# shift right at new parent
		frappe.db.sql(f"""update `tab{doc.doctype}` set lft = lft + %s, rgt = rgt + %s, modified=%s
			where lft > %s""", (diff, diff, current_time, new_parent.rgt))

		# shift right rgts of ancestors whose only rgts must shift
		frappe.db.sql(f"""update `tab{doc.doctype}` set rgt = rgt + %s, modified=%s
			where lft < %s and rgt > %s""",
			(diff, current_time, new_parent.lft, new_parent.rgt))

		new_diff = new_parent.rgt - doc.lft
	else:
		# new root
		max_rgt = frappe.db.sql(f"select max(rgt) from `tab{doc.doctype}`")[0][0]
		new_diff = max_rgt + 1 - doc.lft

	# bring back from dark side
	frappe.db.sql(f"""update `tab{doc.doctype}` set lft = -lft + %s, rgt = -rgt + %s, modified=%s
		where lft < 0""", (new_diff, new_diff, current_time))

@frappe.whitelist()
def rebuild_tree(doctype, parent_field):
	"""Reset lft and rgt domain values for all documents of specified DocType.
	"""

	# Check for perm if called from client-side
	if frappe.request and frappe.local.form_dict.cmd == 'rebuild_tree':
		frappe.only_for('System Manager')

	# get all roots
	frappe.db.auto_commit_on_many_writes = 1

	right = 1
	result = frappe.db.sql("SELECT name FROM `tab%s` WHERE `%s`='' or `%s` IS NULL ORDER BY name ASC" % (doctype, parent_field, parent_field))
	for r in result:
		right = rebuild_node(doctype, r[0], right, parent_field)

	frappe.db.auto_commit_on_many_writes = 0

def rebuild_node(doctype, parent, left, parent_field):
	"""Reset lft and rgt domain values for node and all children recursively.

		:param doctype: doctype name
		:param parent: name of parent document
		:param left: left domain value
		:param parent_field: Link field specifying parent relation.
		:return next left domain value in NSM.

	"""

	from frappe.utils import now
	current_time = now()

	# the right value of this node is the left value + 1
	right = left+1

	# get all children of this node
	result = frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE `{parent_field}`=%s", (parent))
	for r in result:
		right = rebuild_node(doctype, r[0], right, parent_field)

	# we've got the left value, and now that we've processed
	# the children of this node we also know the right value
	frappe.db.sql(f"""UPDATE `tab{doctype}` SET lft=%s, rgt=%s, modified=%s
		WHERE name=%s""", (left,right,current_time,parent))

	#return the right value of this node + 1
	return right+1


def validate_loop(doctype, name, lft, rgt):
	"""Check if document with same name exists as it's ancestor.

		:param doctype: doctype name
		:param name: name of document
		:param lft: left domain value
		:param rgt: right domain value

		throws NestedSetRecursionError if validation fails.
	"""

	if name in frappe.db.sql_list(f"""select name from `tab{doctype}` where lft <= %s and rgt >= %s""", (lft, rgt)):
		frappe.throw(_("Item cannot be added to its own descendents"), NestedSetRecursionError)

class NestedSet(Document):
	"""Tree (Hierarchical) Nested Set Model (nsm)

		To use the nested set model, use the following pattern.
		1. name your parent field as "parent_item_group" if not have a property
			nsm_parent_field as your field name in the document class
		2. have a field called "old_parent" in your fields list - this
			identifies whether the parent has been changed
		3. call update_nsm(doc_obj) in the on_upate method

		Alternatively, you can use "Is tree" checkbox while creating new doctype
		to model tree-like doctypes.

		More on NestedSet data structure : https://en.wikipedia.org/wiki/Nested_set_model#Example
	"""
	def __setup__(self):
		if self.meta.get("nsm_parent_field"):
			self.nsm_parent_field = self.meta.nsm_parent_field

	def on_update(self):
		update_nsm(self)
		self.validate_ledger()

	def on_trash(self, allow_root_deletion=False):
		if not getattr(self, 'nsm_parent_field', None):
			self.nsm_parent_field = frappe.scrub(self.doctype) + "_parent"

		parent = self.get(self.nsm_parent_field)
		if not parent and not allow_root_deletion:
			frappe.throw(_("Root {0} cannot be deleted").format(_(self.doctype)))

		# cannot delete non-empty group
		self.validate_if_child_exists()

		self.set(self.nsm_parent_field, "")

		try:
			update_nsm(self)
		except frappe.DoesNotExistError:
			if self.flags.on_rollback:
				pass
				frappe.message_log.pop()
			else:
				raise

	def validate_if_child_exists(self):
		has_children = frappe.db.sql(f"""select count(name) from `tab{self.doctype}`
			where `{self.nsm_parent_field}`=%s""",
			(self.name,))[0][0]
		if has_children:
			frappe.throw(_("Cannot delete {0} as it has child nodes").format(self.name), NestedSetChildExistsError)

	def before_rename(self, olddn, newdn, merge=False, group_fname="is_group"):
		if merge and hasattr(self, group_fname):
			is_group = frappe.db.get_value(self.doctype, newdn, group_fname)
			if self.get(group_fname) != is_group:
				frappe.throw(_("Merging is only possible between Group-to-Group or Leaf Node-to-Leaf Node"), NestedSetInvalidMergeError)

	def after_rename(self, olddn, newdn, merge=False):
		if not self.nsm_parent_field:
			parent_field = "parent_" + self.doctype.replace(" ", "_").lower()
		else:
			parent_field = self.nsm_parent_field

		# set old_parent for children
		frappe.db.sql(f"update `tab{self.doctype}` set old_parent=%s where {parent_field}=%s", (newdn, newdn))

		if merge:
			rebuild_tree(self.doctype, parent_field)

	def validate_one_root(self):
		if not self.get(self.nsm_parent_field):
			if self.get_root_node_count() > 1:
				frappe.throw(_("""Multiple root nodes not allowed."""), NestedSetMultipleRootsError)

	def get_root_node_count(self):
		return frappe.db.count(self.doctype, {
			self.nsm_parent_field: ''
		})

	def validate_ledger(self, group_identifier="is_group"):
		if hasattr(self, group_identifier) and not bool(self.get(group_identifier)):
			if frappe.db.sql(f"""select name from `tab{self.doctype}` where {self.nsm_parent_field}=%s and docstatus!=2""", (self.name)):
				frappe.throw(_("{0} {1} cannot be a leaf node as it has children").format(_(self.doctype), self.name))

	def get_ancestors(self):
		return get_ancestors_of(self.doctype, self.name)

def get_root_of(doctype):
	"""Get root element of a DocType with a tree structure.

	Only first root is returned if there are multiple roots in a DocType.
	"""
	result = frappe.db.sql(f"""select t1.name from `tab{doctype}` t1 where
		(select count(*) from `tab{doctype}` t2 where
			t2.lft < t1.lft and t2.rgt > t1.rgt) = 0
		and t1.rgt > t1.lft""")
	return result[0][0] if result else None

def get_ancestors_of(doctype, name, order_by="lft desc", limit=None):
	"""Get ancestor elements of a DocType with a tree structure."""
	lft, rgt = frappe.db.get_value(doctype, name, ["lft", "rgt"])

	result = [d["name"] for d in frappe.db.get_all(doctype, {"lft": ["<", lft], "rgt": [">", rgt]},
		"name", order_by=order_by, limit_page_length=limit)]

	return result or []

def get_descendants_of(doctype, name, order_by="lft desc", limit=None,
	ignore_permissions=False):
	"""Get descendants of the specified document recursively."""
	lft, rgt = frappe.db.get_value(doctype, name, ['lft', 'rgt'])

	result = [d["name"] for d in frappe.db.get_list(doctype, {"lft": [">", lft], "rgt": ["<", rgt]},
		"name", order_by=order_by, limit_page_length=limit, ignore_permissions=ignore_permissions)]

	return result or []
