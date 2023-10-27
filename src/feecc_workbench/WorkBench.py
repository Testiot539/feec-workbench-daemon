import asyncio
import pathlib
from pathlib import Path

from loguru import logger

from ._label_generation import create_qr, create_seal_tag
from .Camera import Camera
from .config import CONFIG
from .database import MongoDbWrapper
from .Employee import Employee
from .exceptions import StateForbiddenError
from .ipfs import publish_file
from .Messenger import messenger
from .metrics import metrics
from .models import ProductionSchema
from .passport_generator import construct_unit_passport
from .printer import print_image
from .robonomics import post_to_datalog
from .Singleton import SingletonMeta
from .states import STATE_TRANSITION_MAP, State
from .translation import translation
from .Types import AdditionalInfo
from .Unit import Unit
from .unit_utils import UnitStatus, get_first_unit_matching_status
from .utils import timestamp

STATE_SWITCH_EVENT = asyncio.Event()


class WorkBench(metaclass=SingletonMeta):
    """
    Work bench is a union of an Employee, working at it and Camera attached.
    It provides highly abstract interface for interaction with them
    """

    @logger.catch
    def __init__(self) -> None:
        self._database: MongoDbWrapper = MongoDbWrapper()
        self.number: int = CONFIG.workbench.number
        self.camera: Camera | None = Camera() if CONFIG.camera.enable else None
        self.employee: Employee | None = None
        self.unit: Unit | None = None
        self.state: State = State.AWAIT_LOGIN_STATE

        logger.info(f"Workbench {self.number} was initialized")

    async def _print_unit_barcode(self, unit: Unit) -> None:
        """Print unit barcode"""
        if (schema := unit.schema).parent_schema_id is None:
            annotation = schema.print_name
        else:
            parent_schema = await self._database.get_schema_by_id(schema.parent_schema_id)
            annotation = f"{parent_schema.print_name}. {unit.schema.print_name}."
        assert self.employee is not None
        try:
            await print_image(Path(unit.barcode.filename), annotation=annotation)
        except Exception as e:
            messenger.error(translation('ErrorPrintLabel'))
            raise e
        finally:
            pathlib.Path(unit.barcode.filename).unlink()

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    async def create_new_unit(self, schema: ProductionSchema) -> Unit:
        """initialize a new instance of the Unit class"""
        if self.state != State.AUTHORIZED_IDLING_STATE:
            message = "Cannot create a new unit unless workbench has state AuthorizedIdling"
            messenger.error(translation('AuthorizedState'))
            raise StateForbiddenError(message)
        unit = Unit(schema)
        if CONFIG.printer.print_barcode and CONFIG.printer.enable:
            await self._print_unit_barcode(unit)
        await self._database.push_unit(unit)
        metrics.register_create_unit(self.employee, unit)

        return unit

    def _validate_state_transition(self, new_state: State) -> None:
        """check if state transition can be performed using the map"""
        if new_state not in STATE_TRANSITION_MAP.get(self.state, []):
            message = f"State transition from {self.state.value} to {new_state.value} is not allowed."
            messenger.error(translation('InvalidState'))
            raise StateForbiddenError(message)

    def switch_state(self, new_state: State) -> None:
        """apply new state to the workbench"""
        assert isinstance(new_state, State)
        self._validate_state_transition(new_state)
        logger.info(f"Workbench no.{self.number} state changed: {self.state.value} -> {new_state.value}")
        self.state = new_state
        STATE_SWITCH_EVENT.set()

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    def log_in(self, employee: Employee) -> None:
        """authorize employee"""
        self._validate_state_transition(State.AUTHORIZED_IDLING_STATE)

        self.employee = employee
        message = f"Employee {employee.name} is logged in at the workbench no. {self.number}"
        logger.info(message)
        messenger.success(translation('Authorized') +" "+ employee.position +" "+ employee.name)

        self.switch_state(State.AUTHORIZED_IDLING_STATE)
        metrics.register_log_in(employee)

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    def log_out(self) -> None:
        """log out the employee"""
        self._validate_state_transition(State.AWAIT_LOGIN_STATE)

        if self.state == State.UNIT_ASSIGNED_IDLING_STATE:
            self.remove_unit()

        assert self.employee is not None
        message = f"Employee {self.employee.name} was logged out at the workbench no. {self.number}"
        logger.info(message)
        messenger.success(self.employee.name +" "+ translation('loggedOut'))
        metrics.register_log_out(self.employee)
        self.employee = None

        self.switch_state(State.AWAIT_LOGIN_STATE)

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    def assign_unit(self, unit: Unit) -> None:
        """assign a unit to the workbench"""
        self._validate_state_transition(State.UNIT_ASSIGNED_IDLING_STATE)

        override = unit.status == UnitStatus.built and unit.passport_ipfs_cid is None
        allowed = (UnitStatus.production, UnitStatus.revision)

        if not (override or unit.status in allowed):
            try:
                unit = get_first_unit_matching_status(unit, *allowed)
            except AssertionError as e:
                message = f"Can only assign unit with status: {', '.join(s.value for s in allowed)}. Unit status is {unit.status.value}. Forbidden."
                messenger.warning(translation('CompletedBuild'))
                raise AssertionError(message) from e

        self.unit = unit

        message = f"Unit {unit.internal_id} has been assigned to the workbench"
        logger.info(message)
        messenger.success(translation('ProductIntID') +" "+ unit.internal_id +" "+ translation('OnTable'))

        if not unit.components_filled:
            logger.info(
                f"Unit {unit.internal_id} is a composition with unsatisfied component requirements. Entering component gathering state."
            )
            self.switch_state(State.GATHER_COMPONENTS_STATE)
        else:
            self.switch_state(State.UNIT_ASSIGNED_IDLING_STATE)

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    def remove_unit(self) -> None:
        """remove a unit from the workbench"""
        self._validate_state_transition(State.AUTHORIZED_IDLING_STATE)

        if self.unit is None:
            message = "Cannot remove unit. No unit is currently assigned to the workbench."
            messenger.error(translation('ImpossibleRemove') +" "+ translation('NoProduct'))
            raise AssertionError(message)

        message = f"Unit {self.unit.internal_id} has been removed from the workbench"
        logger.info(message)
        messenger.success(translation('ProductIntID') +" "+ self.unit.internal_id +" "+ translation('ClearTable'))

        self.unit = None

        self.switch_state(State.AUTHORIZED_IDLING_STATE)

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    async def start_operation(self, additional_info: AdditionalInfo) -> None:
        """begin work on the provided unit"""
        self._validate_state_transition(State.PRODUCTION_STAGE_ONGOING_STATE)

        if self.unit is None:
            message = "No unit is assigned to the workbench"
            messenger.error(translation('NoProduct'))
            raise AssertionError(message)

        if self.employee is None:
            message = "No employee is logged in at the workbench"
            messenger.error(translation('NecessaryAuth'))
            raise AssertionError(message)

        if self.camera is not None:
            await self.camera.start_record()

        self.unit.start_operation(self.employee, additional_info)

        self.switch_state(State.PRODUCTION_STAGE_ONGOING_STATE)

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError, ValueError))
    async def assign_component_to_unit(self, component: Unit) -> None:
        """assign provided component to a composite unit"""
        assert (
            self.state == State.GATHER_COMPONENTS_STATE and self.unit is not None
        ), f"Cannot assign components unless WB is in state {State.GATHER_COMPONENTS_STATE}"

        self.unit.assign_component(component)
        STATE_SWITCH_EVENT.set()

        if self.unit.components_filled:
            await self._database.push_unit(self.unit)
            self.switch_state(State.UNIT_ASSIGNED_IDLING_STATE)

    async def _end_record(self) -> tuple[list[str], str]:  # noqa: CAC001
        """End ongoing records and publish them to IPFS"""
        assert self.camera is not None and self.employee is not None
        override_timestamp = timestamp()
        ipfs_hashes: list[str] = []

        try:
            await self.camera.end_record()
            override_timestamp = timestamp()
            assert self.camera.record is not None, "No record found"
            file: str | None = self.camera.record.filename
        except Exception as e:
            logger.error(f"Failed to end record: {e}")
            messenger.warning(translation('NotSaveVideo'))
            file = None

        if file is not None:
            try:
                data = await publish_file(file_path=Path(file), rfid_card_id=self.employee.rfid_card_id)

                if data is not None:
                    cid, link = data
                    ipfs_hashes.append(cid)
            except Exception as e:
                logger.error(f"Failed to publish record: {e}")
                messenger.warning(translation('SaveLocalVideo'))
                ipfs_hashes = []

        return ipfs_hashes, override_timestamp

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    async def end_operation(self, additional_info: AdditionalInfo | None = None, premature: bool = False) -> None:
        """end work on the provided unit"""
        self._validate_state_transition(State.UNIT_ASSIGNED_IDLING_STATE)

        if self.unit is None:
            message = "No unit is assigned to the workbench"
            messenger.error(translation('NoProduct'))
            raise AssertionError(message)

        logger.info("Trying to end operation")
        override_timestamp = timestamp()
        ipfs_hashes: list[str] = []

        if self.camera is not None and self.employee is not None:
            ipfs_hashes, override_timestamp = await self._end_record()

        await self.unit.end_operation(
            video_hashes=ipfs_hashes,
            additional_info=additional_info,
            premature=premature,
            override_timestamp=override_timestamp,
        )
        await self._database.push_unit(self.unit, include_components=False)

        self.switch_state(State.UNIT_ASSIGNED_IDLING_STATE)
        metrics.register_complete_operation(self.employee, self.unit)

    async def _print_security_tag(self) -> None:
        """Print security tag for the unit"""
        assert self.employee is not None
        seal_tag_img: Path = create_seal_tag()
        try:
            await print_image(seal_tag_img, self.employee.rfid_card_id)
        except Exception as e:
            messenger.error(translation('ErrorPrintSeal'))
            logger.error(str(e))
        finally:
            pathlib.Path(seal_tag_img).unlink()

    async def _print_qr(self, url: str) -> None:
        """Print passport QR-code tag for the unit"""
        assert self.employee is not None
        assert self.unit is not None
        self.unit.passport_short_url = url
        qrcode_path = create_qr(url)
        try:
            if self.unit.schema.parent_schema_id is None:
                annotation = f"{self.unit.model_name} (ID: {self.unit.internal_id})."
            else:
                parent_schema = await self._database.get_schema_by_id(self.unit.schema.parent_schema_id)
                annotation = (
                    f"{parent_schema.unit_name}. {self.unit.model_name} (ID: {self.unit.internal_id})."
                )

            await print_image(
                qrcode_path,
                annotation=annotation,
            )
        except Exception as e:
            messenger.error(translation('ErrorPrintQR'))
            logger.error(str(e))
            raise e
        finally:
            pathlib.Path(qrcode_path).unlink()

    @logger.catch(reraise=True, exclude=(StateForbiddenError, AssertionError))
    async def upload_unit_passport(self) -> None:  # noqa: CAC001,CCR001
        """Finalize the Unit's assembly by producing and publishing its passport"""

        # Make sure nothing needed for this operation is missing
        if self.unit is None:
            messenger.error(translation('NoProduct'))
            raise AssertionError("No unit is assigned to the workbench")

        if self.employee is None:
            messenger.error(translation('NecessaryAuth'))
            raise AssertionError("No employee is logged in at the workbench")

        # Generate and save passport YAML file
        passport_file_path: Path = await construct_unit_passport(self.unit)

        # Determine if QR-code has to be printed -> short link is needed right now
        print_qr = CONFIG.printer.print_qr and (
            not CONFIG.printer.print_qr_only_for_composite
            or self.unit.schema.is_composite
            or not self.unit.schema.is_a_component
        )

        # Publish passport YAML file into IPFS
        if CONFIG.ipfs_gateway.enable:
            cid, link = await publish_file(file_path=passport_file_path, rfid_card_id=self.employee.rfid_card_id)
            self.unit.passport_ipfs_cid = cid

            # Generate a QR-code pointing to the unit's passport and print it
            if print_qr:
                try:
                    await self._print_qr(link)
                except Exception as e:
                    messenger.error(translation('CanceledPasport'))
                    logger.error(f"Failed to print QR code. Passport not saved. {e}")
                    raise e

        # Print a security tag sticker if needed
        if CONFIG.printer.print_security_tag:
            await self._print_security_tag()

        # Publish passport file's IPFS CID to Robonomics Datalog
        if CONFIG.robonomics.enable_datalog and (cid := self.unit.passport_ipfs_cid) is not None:
            asyncio.create_task(post_to_datalog(cid, self.unit.internal_id))

        # Update unit data saved in the DB
        await self._database.push_unit(self.unit)
        metrics.register_generate_passport(self.employee, self.unit)

    async def shutdown(self) -> None:
        logger.info("Workbench shutdown sequence initiated")
        messenger.warning(translation('ShutDownServer'))

        if self.state == State.PRODUCTION_STAGE_ONGOING_STATE:
            logger.warning(
                "Ending ongoing operation prematurely. Reason: Unfinished when Workbench shutdown sequence initiated"
            )
            await self.end_operation(
                premature=True,
                additional_info={"Ended reason": "Unfinished when Workbench shutdown sequence initiated"},
            )

        if self.state in (State.UNIT_ASSIGNED_IDLING_STATE, State.GATHER_COMPONENTS_STATE):
            self.remove_unit()

        if self.state == State.AUTHORIZED_IDLING_STATE:
            self.log_out()

        message = "Workbench shutdown sequence complete"
        logger.info(message)
        messenger.success(translation('FinishServer'))
