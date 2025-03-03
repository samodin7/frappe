# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""build query for doclistview and return results"""

import copy
import json
import re
from datetime import datetime

import frappe
import frappe.defaults
import frappe.permissions
import frappe.share
from frappe import _
from frappe.core.doctype.server_script.server_script_utils import get_server_script_map
from frappe.database.utils import FallBackDateTimeStr
from frappe.model import optional_fields
from frappe.model.meta import get_table_columns
from frappe.model.utils.user_settings import get_user_settings, update_user_settings
from frappe.query_builder.utils import Column
from frappe.utils import (
	add_to_date,
	cint,
	cstr,
	flt,
	get_filter,
	get_time,
	get_timespan_date_range,
	make_filter_tuple,
)

LOCATE_PATTERN = re.compile(r"locate\([^,]+,\s*[`\"]?name[`\"]?\s*\)", flags=re.IGNORECASE)
LOCATE_CAST_PATTERN = re.compile(
	r"locate\(([^,]+),\s*([`\"]?name[`\"]?)\s*\)", flags=re.IGNORECASE
)
FUNC_IFNULL_PATTERN = re.compile(
	r"(strpos|ifnull|coalesce)\(\s*[`\"]?name[`\"]?\s*,", flags=re.IGNORECASE
)
CAST_VARCHAR_PATTERN = re.compile(
	r"([`\"]?tab[\w`\" -]+\.[`\"]?name[`\"]?)(?!\w)", flags=re.IGNORECASE
)
ORDER_BY_PATTERN = re.compile(r"\ order\ by\ |\ asc|\ ASC|\ desc|\ DESC", flags=re.IGNORECASE)
SUB_QUERY_PATTERN = re.compile("^.*[,();@].*")
IS_QUERY_PATTERN = re.compile(r"^(select|delete|update|drop|create)\s")
IS_QUERY_PREDICATE_PATTERN = re.compile(
	r"\s*[0-9a-zA-z]*\s*( from | group by | order by | where | join )"
)
FIELD_QUOTE_PATTERN = re.compile(r"[0-9a-zA-Z]+\s*'")
FIELD_COMMA_PATTERN = re.compile(r"[0-9a-zA-Z]+\s*,")
STRICT_FIELD_PATTERN = re.compile(r".*/\*.*")
STRICT_UNION_PATTERN = re.compile(r".*\s(union).*\s")
ORDER_GROUP_PATTERN = re.compile(r".*[^a-z0-9-_ ,`'\"\.\(\)].*")


class DatabaseQuery:
	def __init__(self, doctype, user=None):
		self.doctype = doctype
		self.tables = []
		self.link_tables = []
		self.conditions = []
		self.or_conditions = []
		self.fields = None
		self.user = user or frappe.session.user
		self.ignore_ifnull = False
		self.flags = frappe._dict()
		self.reference_doctype = None

	def execute(
		self,
		fields=None,
		filters=None,
		or_filters=None,
		docstatus=None,
		group_by=None,
		order_by="KEEP_DEFAULT_ORDERING",
		limit_start=False,
		limit_page_length=None,
		as_list=False,
		with_childnames=False,
		debug=False,
		ignore_permissions=False,
		user=None,
		with_comment_count=False,
		join="left join",
		distinct=False,
		start=None,
		page_length=None,
		limit=None,
		ignore_ifnull=False,
		save_user_settings=False,
		save_user_settings_fields=False,
		update=None,
		add_total_row=None,
		user_settings=None,
		reference_doctype=None,
		run=True,
		strict=True,
		pluck=None,
		ignore_ddl=False,
		*,
		parent_doctype=None,
	) -> list:

		if (
			not ignore_permissions
			and not frappe.has_permission(self.doctype, "select", user=user, parent_doctype=parent_doctype)
			and not frappe.has_permission(self.doctype, "read", user=user, parent_doctype=parent_doctype)
		):
			frappe.flags.error_message = _("Insufficient Permission for {0}").format(
				frappe.bold(self.doctype)
			)
			raise frappe.PermissionError(self.doctype)

		# filters and fields swappable
		# its hard to remember what comes first
		if isinstance(fields, dict) or (
			fields and isinstance(fields, list) and isinstance(fields[0], list)
		):
			# if fields is given as dict/list of list, its probably filters
			filters, fields = fields, filters

		elif fields and isinstance(filters, list) and len(filters) > 1 and isinstance(filters[0], str):
			# if `filters` is a list of strings, its probably fields
			filters, fields = fields, filters

		if fields:
			self.fields = fields
		else:
			self.fields = [f"`tab{self.doctype}`.`{pluck or 'name'}`"]

		if start:
			limit_start = start
		if page_length:
			limit_page_length = page_length
		if limit:
			limit_page_length = limit

		self.filters = filters or []
		self.or_filters = or_filters or []
		self.docstatus = docstatus or []
		self.group_by = group_by
		self.order_by = order_by
		self.limit_start = cint(limit_start)
		self.limit_page_length = cint(limit_page_length) if limit_page_length else None
		self.with_childnames = with_childnames
		self.debug = debug
		self.join = join
		self.distinct = distinct
		self.as_list = as_list
		self.ignore_ifnull = ignore_ifnull
		self.flags.ignore_permissions = ignore_permissions
		self.user = user or frappe.session.user
		self.update = update
		self.user_settings_fields = copy.deepcopy(self.fields)
		self.run = run
		self.strict = strict
		self.ignore_ddl = ignore_ddl

		# for contextual user permission check
		# to determine which user permission is applicable on link field of specific doctype
		self.reference_doctype = reference_doctype or self.doctype

		if user_settings:
			self.user_settings = json.loads(user_settings)

		self.columns = self.get_table_columns()

		# no table & ignore_ddl, return
		if not self.columns:
			return []

		result = self.build_and_run()

		if with_comment_count and not as_list and self.doctype:
			self.add_comment_count(result)

		if save_user_settings:
			self.save_user_settings_fields = save_user_settings_fields
			self.update_user_settings()

		if pluck:
			return [d[pluck] for d in result]

		return result

	def build_and_run(self):
		args = self.prepare_args()
		args.limit = self.add_limit()

		if args.conditions:
			args.conditions = "where " + args.conditions

		if self.distinct:
			args.fields = "distinct " + args.fields
			args.order_by = ""  # TODO: recheck for alternative

		# Postgres requires any field that appears in the select clause to also
		# appear in the order by and group by clause
		if frappe.db.db_type == "postgres" and args.order_by and args.group_by:
			args = self.prepare_select_args(args)

		query = (
			"""select %(fields)s
			from %(tables)s
			%(conditions)s
			%(group_by)s
			%(order_by)s
			%(limit)s"""
			% args
		)

		return frappe.db.sql(
			query,
			as_dict=not self.as_list,
			debug=self.debug,
			update=self.update,
			ignore_ddl=self.ignore_ddl,
			run=self.run,
		)

	def prepare_args(self):
		self.parse_args()
		self.sanitize_fields()
		self.extract_tables()
		self.set_optional_columns()
		self.build_conditions()

		args = frappe._dict()

		if self.with_childnames:
			for t in self.tables:
				if t != "`tab" + self.doctype + "`":
					self.fields.append(t + ".name as '%s:name'" % t[4:-1])

		# query dict
		args.tables = self.tables[0]

		# left join parent, child tables
		for child in self.tables[1:]:
			parent_name = cast_name(f"{self.tables[0]}.name")
			args.tables += f" {self.join} {child} on ({child}.parenttype = {frappe.db.escape(self.doctype)} and {child}.parent = {parent_name})"

		# left join link tables
		for link in self.link_tables:
			args.tables += f" {self.join} `tab{link.doctype}` on (`tab{link.doctype}`.`name` = {self.tables[0]}.`{link.fieldname}`)"

		if self.grouped_or_conditions:
			self.conditions.append(f"({' or '.join(self.grouped_or_conditions)})")

		args.conditions = " and ".join(self.conditions)

		if self.or_conditions:
			args.conditions += (" or " if args.conditions else "") + " or ".join(self.or_conditions)

		self.set_field_tables()
		self.cast_name_fields()

		fields = []

		# Wrapping fields with grave quotes to allow support for sql keywords
		# TODO: Add support for wrapping fields with sql functions and distinct keyword
		for field in self.fields:
			stripped_field = field.strip().lower()
			skip_wrapping = any(
				[
					stripped_field.startswith(("`", "*", '"', "'")),
					"(" in stripped_field,
					"distinct" in stripped_field,
				]
			)
			if skip_wrapping:
				fields.append(field)
			elif "as" in field.lower().split(" "):
				col, _, new = field.split()
				fields.append(f"`{col}` as {new}")
			else:
				fields.append(f"`{field}`")

		args.fields = ", ".join(fields)

		self.set_order_by(args)

		self.validate_order_by_and_group_by(args.order_by)
		args.order_by = args.order_by and (" order by " + args.order_by) or ""

		self.validate_order_by_and_group_by(self.group_by)
		args.group_by = self.group_by and (" group by " + self.group_by) or ""

		return args

	def prepare_select_args(self, args):
		order_field = ORDER_BY_PATTERN.sub("", args.order_by)

		if order_field not in args.fields:
			extracted_column = order_column = order_field.replace("`", "")
			if "." in extracted_column:
				extracted_column = extracted_column.split(".")[1]

			args.fields += f", MAX({extracted_column}) as `{order_column}`"
			args.order_by = args.order_by.replace(order_field, f"`{order_column}`")

		return args

	def parse_args(self):
		"""Convert fields and filters from strings to list, dicts"""
		if isinstance(self.fields, str):
			if self.fields == "*":
				self.fields = ["*"]
			else:
				try:
					self.fields = json.loads(self.fields)
				except ValueError:
					self.fields = [f.strip() for f in self.fields.split(",")]

		# remove empty strings / nulls in fields
		self.fields = [f for f in self.fields if f]

		# convert child_table.fieldname to `tabChild DocType`.`fieldname`
		for field in self.fields:
			if "." in field and "tab" not in field:
				original_field = field
				alias = None
				if " as " in field:
					field, alias = field.split(" as ")
				linked_fieldname, fieldname = field.split(".")
				linked_field = frappe.get_meta(self.doctype).get_field(linked_fieldname)
				linked_doctype = linked_field.options
				if linked_field.fieldtype == "Link":
					self.append_link_table(linked_doctype, linked_fieldname)
				field = f"`tab{linked_doctype}`.`{fieldname}`"
				if alias:
					field = f"{field} as {alias}"
				self.fields[self.fields.index(original_field)] = field

		for filter_name in ["filters", "or_filters"]:
			filters = getattr(self, filter_name)
			if isinstance(filters, str):
				filters = json.loads(filters)

			if isinstance(filters, dict):
				fdict = filters
				filters = []
				for key, value in fdict.items():
					filters.append(make_filter_tuple(self.doctype, key, value))
			setattr(self, filter_name, filters)

	def sanitize_fields(self):
		"""
		regex : ^.*[,();].*
		purpose : The regex will look for malicious patterns like `,`, '(', ')', '@', ;' in each
		                field which may leads to sql injection.
		example :
		        field = "`DocType`.`issingle`, version()"
		As field contains `,` and mysql function `version()`, with the help of regex
		the system will filter out this field.
		"""
		blacklisted_keywords = ["select", "create", "insert", "delete", "drop", "update", "case", "show"]
		blacklisted_functions = [
			"concat",
			"concat_ws",
			"if",
			"ifnull",
			"nullif",
			"coalesce",
			"connection_id",
			"current_user",
			"database",
			"last_insert_id",
			"session_user",
			"system_user",
			"user",
			"version",
			"global",
		]

		def _raise_exception():
			frappe.throw(_("Use of sub-query or function is restricted"), frappe.DataError)

		def _is_query(field):
			if IS_QUERY_PATTERN.match(field):
				_raise_exception()

			elif IS_QUERY_PREDICATE_PATTERN.match(field):
				_raise_exception()

		for field in self.fields:
			if SUB_QUERY_PATTERN.match(field):
				if any(f"({keyword}" in field.lower() for keyword in blacklisted_keywords):
					_raise_exception()

				if any(f"{keyword}(" in field.lower() for keyword in blacklisted_functions):
					_raise_exception()

				if "@" in field.lower():
					# prevent access to global variables
					_raise_exception()

			if FIELD_QUOTE_PATTERN.match(field):
				_raise_exception()

			if FIELD_COMMA_PATTERN.match(field):
				_raise_exception()

			_is_query(field)

			if self.strict:
				if STRICT_FIELD_PATTERN.match(field):
					frappe.throw(_("Illegal SQL Query"))

				if STRICT_UNION_PATTERN.match(field.lower()):
					frappe.throw(_("Illegal SQL Query"))

	def extract_tables(self):
		"""extract tables from fields"""
		self.tables = [f"`tab{self.doctype}`"]
		sql_functions = [
			"dayofyear(",
			"extract(",
			"locate(",
			"strpos(",
			"count(",
			"sum(",
			"avg(",
		]
		# add tables from fields
		if self.fields:
			for field in self.fields:
				if not ("tab" in field and "." in field) or any(x for x in sql_functions if x in field):
					continue

				table_name = field.split(".")[0]

				if table_name.lower().startswith("group_concat("):
					table_name = table_name[13:]
				if not table_name[0] == "`":
					table_name = f"`{table_name}`"
				if table_name not in self.tables and table_name not in (
					d.table_name for d in self.link_tables
				):
					self.append_table(table_name)

	def append_table(self, table_name):
		self.tables.append(table_name)
		doctype = table_name[4:-1]
		self.check_read_permission(doctype)

	def append_link_table(self, doctype, fieldname):
		for d in self.link_tables:
			if d.doctype == doctype and d.fieldname == fieldname:
				return

		self.check_read_permission(doctype)
		self.link_tables.append(
			frappe._dict(doctype=doctype, fieldname=fieldname, table_name=f"`tab{doctype}`")
		)

	def check_read_permission(self, doctype):
		ptype = "select" if frappe.only_has_select_perm(doctype) else "read"

		if not self.flags.ignore_permissions and not frappe.has_permission(
			doctype, ptype=ptype, parent_doctype=self.doctype
		):
			frappe.flags.error_message = _("Insufficient Permission for {0}").format(frappe.bold(doctype))
			raise frappe.PermissionError(doctype)

	def set_field_tables(self):
		"""If there are more than one table, the fieldname must not be ambiguous.
		If the fieldname is not explicitly mentioned, set the default table"""

		def _in_standard_sql_methods(field):
			methods = ("count(", "avg(", "sum(", "extract(", "dayofyear(")
			return field.lower().startswith(methods)

		if len(self.tables) > 1 or len(self.link_tables) > 0:
			for idx, field in enumerate(self.fields):
				if "." not in field and not _in_standard_sql_methods(field):
					self.fields[idx] = f"{self.tables[0]}.{field}"

	def cast_name_fields(self):
		for i, field in enumerate(self.fields):
			self.fields[i] = cast_name(field)

	def get_table_columns(self):
		try:
			return get_table_columns(self.doctype)
		except frappe.db.TableMissingError:
			if self.ignore_ddl:
				return None
			else:
				raise

	def set_optional_columns(self):
		"""Removes optional columns like `_user_tags`, `_comments` etc. if not in table"""
		# remove from fields
		to_remove = []
		for fld in self.fields:
			for f in optional_fields:
				if f in fld and not f in self.columns:
					to_remove.append(fld)

		for fld in to_remove:
			del self.fields[self.fields.index(fld)]

		# remove from filters
		to_remove = []
		for each in self.filters:
			if isinstance(each, str):
				each = [each]

			for element in each:
				if element in optional_fields and element not in self.columns:
					to_remove.append(each)

		for each in to_remove:
			if isinstance(self.filters, dict):
				del self.filters[each]
			else:
				self.filters.remove(each)

	def build_conditions(self):
		self.conditions = []
		self.grouped_or_conditions = []
		self.build_filter_conditions(self.filters, self.conditions)
		self.build_filter_conditions(self.or_filters, self.grouped_or_conditions)

		# match conditions
		if not self.flags.ignore_permissions:
			match_conditions = self.build_match_conditions()
			if match_conditions:
				self.conditions.append(f"({match_conditions})")

	def build_filter_conditions(self, filters, conditions, ignore_permissions=None):
		"""build conditions from user filters"""
		if ignore_permissions is not None:
			self.flags.ignore_permissions = ignore_permissions

		if isinstance(filters, dict):
			filters = [filters]

		for f in filters:
			if isinstance(f, str):
				conditions.append(f)
			else:
				conditions.append(self.prepare_filter_condition(f))

	def prepare_filter_condition(self, f):
		"""Returns a filter condition in the format:
		ifnull(`tabDocType`.`fieldname`, fallback) operator "value"
		"""

		# TODO: refactor

		from frappe.boot import get_additional_filters_from_hooks

		additional_filters_config = get_additional_filters_from_hooks()
		f = get_filter(self.doctype, f, additional_filters_config)

		tname = "`tab" + f.doctype + "`"
		if tname not in self.tables:
			self.append_table(tname)

		column_name = cast_name(f.fieldname if "ifnull(" in f.fieldname else f"{tname}.`{f.fieldname}`")

		if f.operator.lower() in additional_filters_config:
			f.update(get_additional_filter_field(additional_filters_config, f, f.value))

		meta = frappe.get_meta(f.doctype)
		can_be_null = True

		# prepare in condition
		if f.operator.lower() in (
			"ancestors of",
			"descendants of",
			"not ancestors of",
			"not descendants of",
		):
			values = f.value or ""

			# TODO: handle list and tuple
			# if not isinstance(values, (list, tuple)):
			# 	values = values.split(",")

			field = meta.get_field(f.fieldname)
			ref_doctype = field.options if field else f.doctype

			lft, rgt = "", ""
			if f.value:
				lft, rgt = frappe.db.get_value(ref_doctype, f.value, ["lft", "rgt"])

			# Get descendants elements of a DocType with a tree structure
			if f.operator.lower() in ("descendants of", "not descendants of"):
				result = frappe.get_all(
					ref_doctype, filters={"lft": [">", lft], "rgt": ["<", rgt]}, order_by="`lft` ASC"
				)
			else:
				# Get ancestor elements of a DocType with a tree structure
				result = frappe.get_all(
					ref_doctype, filters={"lft": ["<", lft], "rgt": [">", rgt]}, order_by="`lft` DESC"
				)

			fallback = "''"
			value = [frappe.db.escape((cstr(v.name) or "").strip(), percent=False) for v in result]
			if len(value):
				value = f"({', '.join(value)})"
			else:
				value = "('')"

			# changing operator to IN as the above code fetches all the parent / child values and convert into tuple
			# which can be directly used with IN operator to query.
			f.operator = (
				"not in" if f.operator.lower() in ("not ancestors of", "not descendants of") else "in"
			)

		elif f.operator.lower() in ("in", "not in"):
			# if values contain '' or falsy values then only coalesce column
			# for `in` query this is only required if values contain '' or values are empty.
			# for `not in` queries we can't be sure as column values might contain null.
			if f.operator.lower() == "in":
				can_be_null = not f.value or any(v is None or v == "" for v in f.value)

			values = f.value or ""
			if isinstance(values, str):
				values = values.split(",")

			fallback = "''"
			value = [frappe.db.escape((cstr(v) or "").strip(), percent=False) for v in values]
			if len(value):
				value = f"({', '.join(value)})"
			else:
				value = "('')"

		else:
			df = meta.get("fields", {"fieldname": f.fieldname})
			df = df[0] if df else None

			if df and df.fieldtype in ("Check", "Float", "Int", "Currency", "Percent"):
				can_be_null = False

			if f.operator.lower() in ("previous", "next", "timespan"):
				date_range = get_date_range(f.operator.lower(), f.value)
				f.operator = "Between"
				f.value = date_range
				fallback = f"'{FallBackDateTimeStr}'"

			if f.operator in (">", "<") and (f.fieldname in ("creation", "modified")):
				value = cstr(f.value)
				fallback = f"'{FallBackDateTimeStr}'"

			elif f.operator.lower() in ("between") and (
				f.fieldname in ("creation", "modified")
				or (df and (df.fieldtype == "Date" or df.fieldtype == "Datetime"))
			):

				value = get_between_date_filter(f.value, df)
				fallback = f"'{FallBackDateTimeStr}'"

			elif f.operator.lower() == "is":
				if f.value == "set":
					f.operator = "!="
				elif f.value == "not set":
					f.operator = "="

				value = ""
				fallback = "''"
				can_be_null = True

				if "ifnull" not in column_name.lower():
					column_name = f"ifnull({column_name}, {fallback})"

			elif df and df.fieldtype == "Date":
				value = frappe.db.format_date(f.value)
				fallback = "'0001-01-01'"

			elif (df and df.fieldtype == "Datetime") or isinstance(f.value, datetime):
				value = frappe.db.format_datetime(f.value)
				fallback = f"'{FallBackDateTimeStr}'"

			elif df and df.fieldtype == "Time":
				value = get_time(f.value).strftime("%H:%M:%S.%f")
				fallback = "'00:00:00'"

			elif f.operator.lower() in ("like", "not like") or (
				isinstance(f.value, str)
				and (not df or df.fieldtype not in ["Float", "Int", "Currency", "Percent", "Check"])
			):
				value = "" if f.value is None else f.value
				fallback = "''"

				if f.operator.lower() in ("like", "not like") and isinstance(value, str):
					# because "like" uses backslash (\) for escaping
					value = value.replace("\\", "\\\\").replace("%", "%%")

			elif (
				f.operator == "=" and df and df.fieldtype in ["Link", "Data"]
			):  # TODO: Refactor if possible
				value = f.value or "''"
				fallback = "''"

			elif f.fieldname == "name":
				value = f.value or "''"
				fallback = "''"

			else:
				value = flt(f.value)
				fallback = 0

			if isinstance(f.value, Column):
				can_be_null = False  # added to avoid the ifnull/coalesce addition
				quote = '"' if frappe.conf.db_type == "postgres" else "`"
				value = f"{tname}.{quote}{f.value.name}{quote}"

			# escape value
			elif isinstance(value, str) and f.operator.lower() != "between":
				value = f"{frappe.db.escape(value, percent=False)}"

		if (
			self.ignore_ifnull
			or not can_be_null
			or (f.value and f.operator.lower() in ("=", "like"))
			or "ifnull(" in column_name.lower()
		):
			if f.operator.lower() == "like" and frappe.conf.get("db_type") == "postgres":
				f.operator = "ilike"
			condition = f"{column_name} {f.operator} {value}"
		else:
			condition = f"ifnull({column_name}, {fallback}) {f.operator} {value}"

		return condition

	def build_match_conditions(self, as_condition=True) -> str | list:
		"""add match conditions if applicable"""
		self.match_filters = []
		self.match_conditions = []
		only_if_shared = False
		if not self.user:
			self.user = frappe.session.user

		if not self.tables:
			self.extract_tables()

		meta = frappe.get_meta(self.doctype)
		role_permissions = frappe.permissions.get_role_permissions(meta, user=self.user)
		self.shared = frappe.share.get_shared(self.doctype, self.user)

		if (
			not meta.istable
			and not (role_permissions.get("select") or role_permissions.get("read"))
			and not self.flags.ignore_permissions
			and not has_any_user_permission_for_doctype(self.doctype, self.user, self.reference_doctype)
		):
			only_if_shared = True
			if not self.shared:
				frappe.throw(_("No permission to read {0}").format(_(self.doctype)), frappe.PermissionError)
			else:
				self.conditions.append(self.get_share_condition())

		else:
			# skip user perm check if owner constraint is required
			if requires_owner_constraint(role_permissions):
				self.match_conditions.append(
					f"`tab{self.doctype}`.`owner` = {frappe.db.escape(self.user, percent=False)}"
				)

			# add user permission only if role has read perm
			elif role_permissions.get("read") or role_permissions.get("select"):
				# get user permissions
				user_permissions = frappe.permissions.get_user_permissions(self.user)
				self.add_user_permissions(user_permissions)

		if as_condition:
			conditions = ""
			if self.match_conditions:
				# will turn out like ((blog_post in (..) and blogger in (...)) or (blog_category in (...)))
				conditions = "((" + ") or (".join(self.match_conditions) + "))"

			doctype_conditions = self.get_permission_query_conditions()
			if doctype_conditions:
				conditions += (" and " + doctype_conditions) if conditions else doctype_conditions

			# share is an OR condition, if there is a role permission
			if not only_if_shared and self.shared and conditions:
				conditions = f"({conditions}) or ({self.get_share_condition()})"

			return conditions

		else:
			return self.match_filters

	def get_share_condition(self):
		return (
			cast_name(f"`tab{self.doctype}`.name")
			+ f" in ({', '.join(frappe.db.escape(s, percent=False) for s in self.shared)})"
		)

	def add_user_permissions(self, user_permissions):
		meta = frappe.get_meta(self.doctype)
		doctype_link_fields = []
		doctype_link_fields = meta.get_link_fields()

		# append current doctype with fieldname as 'name' as first link field
		doctype_link_fields.append(
			dict(
				options=self.doctype,
				fieldname="name",
			)
		)

		match_filters = {}
		match_conditions = []
		for df in doctype_link_fields:
			if df.get("ignore_user_permissions"):
				continue

			user_permission_values = user_permissions.get(df.get("options"), {})

			if user_permission_values:
				docs = []
				if frappe.get_system_settings("apply_strict_user_permissions"):
					condition = ""
				else:
					empty_value_condition = cast_name(
						f"ifnull(`tab{self.doctype}`.`{df.get('fieldname')}`, '')=''"
					)
					condition = empty_value_condition + " or "

				for permission in user_permission_values:
					if not permission.get("applicable_for"):
						docs.append(permission.get("doc"))

					# append docs based on user permission applicable on reference doctype
					# this is useful when getting list of docs from a link field
					# in this case parent doctype of the link
					# will be the reference doctype

					elif df.get("fieldname") == "name" and self.reference_doctype:
						if permission.get("applicable_for") == self.reference_doctype:
							docs.append(permission.get("doc"))

					elif permission.get("applicable_for") == self.doctype:
						docs.append(permission.get("doc"))

				if docs:
					values = ", ".join(frappe.db.escape(doc, percent=False) for doc in docs)
					condition += cast_name(f"`tab{self.doctype}`.`{df.get('fieldname')}`") + f" in ({values})"
					match_conditions.append(f"({condition})")
					match_filters[df.get("options")] = docs

		if match_conditions:
			self.match_conditions.append(" and ".join(match_conditions))

		if match_filters:
			self.match_filters.append(match_filters)

	def get_permission_query_conditions(self):
		conditions = []
		condition_methods = frappe.get_hooks("permission_query_conditions", {}).get(self.doctype, [])
		if condition_methods:
			for method in condition_methods:
				c = frappe.call(frappe.get_attr(method), self.user)
				if c:
					conditions.append(c)

		permision_script_name = get_server_script_map().get("permission_query", {}).get(self.doctype)
		if permision_script_name:
			script = frappe.get_doc("Server Script", permision_script_name)
			condition = script.get_permission_query_conditions(self.user)
			if condition:
				conditions.append(condition)

		return " and ".join(conditions) if conditions else ""

	def set_order_by(self, args):
		meta = frappe.get_meta(self.doctype)

		if self.order_by and self.order_by != "KEEP_DEFAULT_ORDERING":
			args.order_by = self.order_by
		else:
			args.order_by = ""

			# don't add order by from meta if a mysql group function is used without group by clause
			group_function_without_group_by = (
				len(self.fields) == 1
				and (
					self.fields[0].lower().startswith("count(")
					or self.fields[0].lower().startswith("min(")
					or self.fields[0].lower().startswith("max(")
				)
				and not self.group_by
			)

			if not group_function_without_group_by:
				sort_field = sort_order = None
				if meta.sort_field and "," in meta.sort_field:
					# multiple sort given in doctype definition
					# Example:
					# `idx desc, modified desc`
					# will covert to
					# `tabItem`.`idx` desc, `tabItem`.`modified` desc
					args.order_by = ", ".join(
						f"`tab{self.doctype}`.`{f.split()[0].strip()}` {f.split()[1].strip()}"
						for f in meta.sort_field.split(",")
					)
				else:
					sort_field = meta.sort_field or "modified"
					sort_order = (meta.sort_field and meta.sort_order) or "desc"
					if self.order_by:
						args.order_by = f"`tab{self.doctype}`.`{sort_field or 'modified'}` {sort_order or 'desc'}"

				# draft docs always on top
				if hasattr(meta, "is_submittable") and meta.is_submittable:
					if self.order_by:
						args.order_by = f"`tab{self.doctype}`.docstatus asc, {args.order_by}"

	def validate_order_by_and_group_by(self, parameters):
		"""Check order by, group by so that atleast one column is selected and does not have subquery"""
		if not parameters:
			return

		_lower = parameters.lower()
		if "select" in _lower and "from" in _lower:
			frappe.throw(_("Cannot use sub-query in order by"))

		if ORDER_GROUP_PATTERN.match(_lower):
			frappe.throw(_("Illegal SQL Query"))

		for field in parameters.split(","):
			if "." in field and field.strip().startswith("`tab"):
				tbl = field.strip().split(".")[0]
				if tbl not in self.tables:
					if tbl.startswith("`"):
						tbl = tbl[4:-1]
					frappe.throw(_("Please select atleast 1 column from {0} to sort/group").format(tbl))

	def add_limit(self):
		if self.limit_page_length:
			return f"limit {self.limit_page_length} offset {self.limit_start}"
		else:
			return ""

	def add_comment_count(self, result):
		for r in result:
			if not r.name:
				continue

			r._comment_count = 0
			if "_comments" in r:
				r._comment_count = len(json.loads(r._comments or "[]"))

	def update_user_settings(self):
		# update user settings if new search
		user_settings = json.loads(get_user_settings(self.doctype))

		if hasattr(self, "user_settings"):
			user_settings.update(self.user_settings)

		if self.save_user_settings_fields:
			user_settings["fields"] = self.user_settings_fields

		update_user_settings(self.doctype, user_settings)


def cast_name(column: str) -> str:
	"""Casts name field to varchar for postgres

	Handles majorly 4 cases:
	1. locate
	2. strpos
	3. ifnull
	4. coalesce

	Uses regex substitution.

	Example:
	input - "ifnull(`tabBlog Post`.`name`, '')=''"
	output - "ifnull(cast(`tabBlog Post`.`name` as varchar), '')=''" """

	if frappe.db.db_type == "mariadb":
		return column

	kwargs = {"string": column}
	if "cast(" not in column.lower() and "::" not in column:
		if LOCATE_PATTERN.search(**kwargs):
			return LOCATE_CAST_PATTERN.sub(r"locate(\1, cast(\2 as varchar))", **kwargs)

		elif match := FUNC_IFNULL_PATTERN.search(**kwargs):
			func = match.groups()[0]
			return re.sub(rf"{func}\(\s*([`\"]?name[`\"]?)\s*,", rf"{func}(cast(\1 as varchar),", **kwargs)

		return CAST_VARCHAR_PATTERN.sub(r"cast(\1 as varchar)", **kwargs)

	return column


def check_parent_permission(parent, child_doctype):
	if parent:
		# User may pass fake parent and get the information from the child table
		if child_doctype and not (
			frappe.db.exists("DocField", {"parent": parent, "options": child_doctype})
			or frappe.db.exists("Custom Field", {"dt": parent, "options": child_doctype})
		):
			raise frappe.PermissionError

		if frappe.permissions.has_permission(parent):
			return

	# Either parent not passed or the user doesn't have permission on parent doctype of child table!
	raise frappe.PermissionError


def get_order_by(doctype, meta):
	order_by = ""

	sort_field = sort_order = None
	if meta.sort_field and "," in meta.sort_field:
		# multiple sort given in doctype definition
		# Example:
		# `idx desc, modified desc`
		# will covert to
		# `tabItem`.`idx` desc, `tabItem`.`modified` desc
		order_by = ", ".join(
			f"`tab{doctype}`.`{f.split()[0].strip()}` {f.split()[1].strip()}"
			for f in meta.sort_field.split(",")
		)

	else:
		sort_field = meta.sort_field or "modified"
		sort_order = (meta.sort_field and meta.sort_order) or "desc"
		order_by = f"`tab{doctype}`.`{sort_field or 'modified'}` {sort_order or 'desc'}"

	# draft docs always on top
	if meta.is_submittable:
		order_by = f"`tab{doctype}`.docstatus asc, {order_by}"

	return order_by


def is_parent_only_filter(doctype, filters):
	# check if filters contains only parent doctype
	only_parent_doctype = True

	if isinstance(filters, list):
		for filter in filters:
			if doctype not in filter:
				only_parent_doctype = False
			if "Between" in filter:
				filter[3] = get_between_date_filter(flt[3])

	return only_parent_doctype


def has_any_user_permission_for_doctype(doctype, user, applicable_for):
	user_permissions = frappe.permissions.get_user_permissions(user=user)
	doctype_user_permissions = user_permissions.get(doctype, [])

	for permission in doctype_user_permissions:
		if not permission.applicable_for or permission.applicable_for == applicable_for:
			return True

	return False


def get_between_date_filter(value, df=None):
	"""
	return the formattted date as per the given example
	[u'2017-11-01', u'2017-11-03'] => '2017-11-01 00:00:00.000000' AND '2017-11-04 00:00:00.000000'
	"""
	from_date = frappe.utils.nowdate()
	to_date = frappe.utils.nowdate()

	if value and isinstance(value, (list, tuple)):
		if len(value) >= 1:
			from_date = value[0]
		if len(value) >= 2:
			to_date = value[1]

	if not df or (df and df.fieldtype == "Datetime"):
		to_date = add_to_date(to_date, days=1)

	if df and df.fieldtype == "Datetime":
		data = "'{}' AND '{}'".format(
			frappe.db.format_datetime(from_date),
			frappe.db.format_datetime(to_date),
		)
	else:
		data = f"'{frappe.db.format_date(from_date)}' AND '{frappe.db.format_date(to_date)}'"

	return data


def get_additional_filter_field(additional_filters_config, f, value):
	additional_filter = additional_filters_config[f.operator.lower()]
	f = frappe._dict(frappe.get_attr(additional_filter["get_field"])())
	if f.query_value:
		for option in f.options:
			option = frappe._dict(option)
			if option.value == value:
				f.value = option.query_value
	return f


def get_date_range(operator: str, value: str):
	timespan_map = {
		"1 week": "week",
		"1 month": "month",
		"3 months": "quarter",
		"6 months": "6 months",
		"1 year": "year",
	}
	period_map = {
		"previous": "last",
		"next": "next",
	}

	if operator != "timespan":
		timespan = f"{period_map[operator]} {timespan_map[value]}"
	else:
		timespan = value

	return get_timespan_date_range(timespan)


def requires_owner_constraint(role_permissions):
	"""Returns True if "select" or "read" isn't available without being creator."""

	if not role_permissions.get("has_if_owner_enabled"):
		return

	if_owner_perms = role_permissions.get("if_owner")
	if not if_owner_perms:
		return

	# has select or read without if owner, no need for constraint
	for perm_type in ("select", "read"):
		if role_permissions.get(perm_type) and perm_type not in if_owner_perms:
			return

	# not checking if either select or read if present in if_owner_perms
	# because either of those is required to perform a query
	return True
