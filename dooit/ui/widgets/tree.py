from typing import Any, List, Literal, Optional, Type, Union
from textual.app import ComposeResult
from textual.reactive import Reactive
from textual.widget import Widget

from dooit.api import Workspace
from dooit.api.model import Model
from dooit.ui.events.events import (
    ChangeStatus,
    CommitData,
    Notify,
    SpawnHelp,
    StatusType,
)
from dooit.ui.widgets.empty import EmptyWidget
from dooit.ui.widgets.sort_options import SortOptions
from dooit.ui.widgets.status_bar_utils import Searcher
from dooit.ui.widgets.todo import TodoWidget
from dooit.ui.widgets.workspace import WorkspaceWidget
from dooit.utils.keybinder import KeyBinder

PRINTABLE = (
    "0123456789"
    + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    + "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ "
)


class Tree(Widget):
    current = Reactive(None)
    key_manager = KeyBinder()
    ModelType = Workspace
    WidgetType = WorkspaceWidget

    DEFAULT_CSS = """
    Tree {
        overflow: auto auto;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, model: Model, classes: str = ""):
        super().__init__(id=f"Tree-{model.uuid}", classes=classes)
        self.model = model
        self.border_title = self.__class__.__name__.replace(
            "Tree", "s"
        )  # Making it plural

    @property
    def current_widget(self) -> WidgetType:
        if isinstance(self.current, Reactive):
            val = self.current._default
        else:
            val = self.current

        return self.get_widget_by_id(val)

    @property
    def node(self) -> ModelType:
        return self.current_widget.model

    @property
    def nodes(self) -> List[WidgetType]:
        return [
            widget
            for widget in self.walk_children()
            if isinstance(widget, self.WidgetType) and widget.display
        ]

    @property
    def model_class_kind(self) -> Literal["workspace", "todo"]:
        raise NotImplementedError

    @property
    def widget_type(self) -> Union[Type[WorkspaceWidget], Type[TodoWidget]]:
        raise NotImplementedError

    def get_children(self, parent: Model) -> List[ModelType]:
        return parent.workspaces

    def get_widget_by_id(self, id_: Any) -> WidgetType:
        return self.query_one(
            f"#{id_}",
            expect_type=self.WidgetType,
        )

    def change_highlights(self, old, new):
        with self.app.batch_update():
            if old:
                try:
                    self.get_widget_by_id(old).highlight(False)
                except Exception:
                    pass

            if new:
                try:
                    widget = self.get_widget_by_id(new)
                    widget.highlight()
                except Exception:
                    self.post_message(Notify("cant find old highlighted node"))

    async def watch_current(self, old: Optional[str], new: Optional[str]):
        self.change_highlights(old, new)

    def compose(self) -> ComposeResult:
        children = self.get_children(self.model)

        if not children:
            yield EmptyWidget(self.model_class_kind)

        for i in children:
            yield self.WidgetType(i)

    async def force_refresh(self, model: Optional[Model] = None):
        was_expanded = dict()
        for i in self.query("*"):
            was_expanded[i.id] = getattr(i, "expanded", False)
            i.remove()

        highlighted = self.current
        if model:
            self.model = model

        children = self.get_children(self.model)

        if not children:
            self.current = None
            await self.mount(EmptyWidget(self.model_class_kind))
            return

        with self.app.batch_update():
            for i in children:
                widget = self.WidgetType(i)
                await self.mount(widget)

                if was_expanded.get(i.uuid, False):
                    widget.toggle_expand()

        self.current = None
        try:
            self.current = self.get_widget_by_id(highlighted).id
        except Exception:
            pass

    async def notify(self, message: str):
        self.post_message(Notify(message))

    def next_node(self) -> Optional[str]:
        nodes = self.nodes

        if not self.current:
            return nodes[0].id if nodes else None

        idx = nodes.index(self.current_widget)
        if idx == len(nodes) - 1:
            return

        return nodes[idx + 1].id

    def prev_node(self):
        nodes = self.nodes

        if not self.current:
            return

        idx = nodes.index(self.current_widget)
        if not idx:
            return

        return nodes[idx - 1].id

    async def shift_node(self, position: Literal["up", "down"]):
        node = self.node

        sibling = node.next_sibling() if position == "down" else node.prev_sibling()

        if sibling:
            sibling_id = sibling.uuid

            if position == "down":
                node.shift_down()
            else:
                node.shift_up()

            new_widget = self.WidgetType(node)
            self.current_widget.remove()

            sibling_widget = self.get_widget_by_id(sibling_id)
            if position == "down":
                await self.mount(new_widget, after=sibling_widget)
            else:
                await self.mount(new_widget, before=sibling_widget)

            self.current = node.uuid
            new_widget.highlight()
            self.post_message(CommitData())

    async def add_first_child(self):
        for i in self.query(EmptyWidget):
            self.styles.overflow_y = "auto"
            self.styles.overflow_x = "auto"
            await i.remove()

        child = self.model.add_child(self.ModelType.class_kind)
        new_widget = self.WidgetType(child)
        await self.mount(new_widget)
        self.current = new_widget.id
        await self.start_edit("description")

    async def add_node(self, type_: Literal["child", "sibling"]):
        if not self.get_children(self.model) or not self.current:
            return await self.add_first_child()

        if type_ == "child" and not self.current_widget.expanded:
            self.current_widget.toggle_expand()

        new_node = (
            self.node.add_child(self.ModelType.class_kind)
            if type_ == "child"
            else self.node.add_sibling()
        )

        widget = self.WidgetType(new_node)
        if type_ == "sibling":
            await self.mount(widget, after=self.current_widget)
        else:
            await self.current_widget.mount(widget)

        self.current = new_node.uuid
        widget.start_edit("description")

    async def remove_item(self):
        if not self.current:
            return

        widget = self.current_widget

        if id_ := self.next_node():
            self.current = id_
        elif id_ := self.prev_node():
            self.current = id_
        else:
            self.current = None

            # NOTE: This hack is done because the app first renders
            # and then removes scrollbars which looks like a glitch

            self.styles.overflow_y = "hidden"
            self.styles.overflow_x = "hidden"
            await self.mount(EmptyWidget(self.model_class_kind))

        widget.model.drop()
        await widget.remove()
        self.post_message(CommitData())

    async def move_down(self):
        if id_ := self.next_node():
            self.current = id_

    async def move_up(self):
        if id_ := self.prev_node():
            self.current = id_

    async def move_to_top(self):
        if self.nodes:
            self.current = self.nodes[0].id

    async def move_to_bottom(self):
        if self.nodes:
            self.current = self.nodes[-1].id

    async def shift_down(self):
        return await self.shift_node("down")

    async def shift_up(self):
        return await self.shift_node("up")

    async def add_child(self):
        await self.add_node("child")

    async def add_sibling(self):
        await self.add_node("sibling")

    async def toggle_expand(self):
        self.current_widget.toggle_expand()

    async def toggle_expand_parent(self):
        id_ = self.current_widget.toggle_expand_parent()
        self.current = id_
        await self.toggle_expand()

    async def copy_text(self):
        await self.current_widget.copy_text()

    async def switch_pane(self):
        pass

    async def start_edit(self, field: str) -> None:
        self.current_widget.start_edit(field)

    async def apply_filter(self, filter):
        for i in self.query(self.widget_type):
            await i.apply_filter(filter)

    async def apply_sort(self, method):
        self.current_widget.model.sort(method)
        if parent := self.current_widget.parent:
            await parent.force_refresh()

        self.post_message(ChangeStatus("NORMAL"))

    async def sort_menu_toggle(self):
        if query := self.query(SortOptions):
            query.first().remove()
            for i in self.query("*"):
                i.remove_class("sort-hide")

            self.post_message(ChangeStatus("NORMAL"))
        else:
            for i in self.query("*"):
                i.add_class("sort-hide")

            await self.mount(SortOptions(self.ModelType, self.current_widget.model))

    async def spawn_help(self):
        self.post_message(SpawnHelp())

    async def change_status(self, status: StatusType):
        self.post_message(ChangeStatus(status))

    async def start_search(self):
        if widget := self.app.query(Searcher):
            widget.first().start_edit()
        else:
            await self.app.query_one("StatusBar").start_search()

    async def stop_search(self):
        pass

    async def keypress(self, key: str):
        if query := self.app.query(Searcher):
            widget = query.first()
            if widget.is_editing:
                await widget.keypress(key)
                return

        if query := self.query(SortOptions):
            await query.first().keypress(key)
            return

        if self.current and self.current_widget._is_editing():
            await self.current_widget.keypress(key)
            return

        self.key_manager.attach_key(key)
        bind = self.key_manager.get_method()
        if bind:
            if hasattr(self, bind.func_name):
                func = getattr(self, bind.func_name)
                if bind.check_for_cursor and self.current == -1:
                    return

                await self.change_status("NORMAL")
                await func(*bind.params)
