from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.models.prompts import PromptForm
from open_webui.routers import prompts


class TestPrompts(AbstractPostgresTest):
    BASE_PATH = "/api/v1/prompts"

    def test_prompts(self):
        create_request = self.make_request(self.create_url("/create"), method="POST")

        with mock_webui_user(id="2", role="admin") as user:
            response = self.run_async(prompts.get_prompts(user=user, db=self.db))
            assert response == []

            created = self.run_async(
                prompts.create_new_prompt(
                    request=create_request,
                    form_data=PromptForm(
                        command="/my-command",
                        title="Hello World",
                        content="description",
                    ),
                    user=user,
                    db=self.db,
                )
            )
            assert created.command == "/my-command"
            assert created.user_id == "2"

            created = self.run_async(
                prompts.create_new_prompt(
                    request=create_request,
                    form_data=PromptForm(
                        command="/my-command2",
                        title="Hello World 2",
                        content="description 2",
                    ),
                    user=user,
                    db=self.db,
                )
            )
            assert created.command == "/my-command2"

            response = self.run_async(prompts.get_prompts(user=user, db=self.db))
            assert len(response) == 2

            fetched = self.run_async(
                prompts.get_prompt_by_command(
                    command="my-command",
                    user=user,
                    db=self.db,
                )
            )
            assert fetched.command == "/my-command"
            assert fetched.title == "Hello World"
            assert fetched.content == "description"
            assert fetched.user_id == "2"
            assert fetched.write_access is True

            updated = self.run_async(
                prompts.update_prompt_by_command(
                    command="my-command2",
                    form_data=PromptForm(
                        command="irrelevant for request",
                        title="Hello World Updated",
                        content="description Updated",
                    ),
                    user=user,
                    db=self.db,
                )
            )
            assert updated.command == "/my-command2"
            assert updated.title == "Hello World Updated"
            assert updated.content == "description Updated"
            assert updated.user_id == "2"

            fetched = self.run_async(
                prompts.get_prompt_by_command(
                    command="my-command2",
                    user=user,
                    db=self.db,
                )
            )
            assert fetched.command == "/my-command2"
            assert fetched.title == "Hello World Updated"
            assert fetched.content == "description Updated"

            deleted = self.run_async(
                prompts.delete_prompt_by_command(
                    command="my-command",
                    user=user,
                    db=self.db,
                )
            )
            assert deleted is True

            response = self.run_async(prompts.get_prompts(user=user, db=self.db))
            assert len(response) == 1
            assert response[0].command == "/my-command2"
