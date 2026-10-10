package BenchtopImager {
    package Requirements {
        requirement <'SR-001'> runTime {
            doc /* The instrument shall deliver analysis results within 2 hours of the start of a run, for a full sample load*/
        }
        requirement <'SR-002'> operatingTemperature {
            doc /* The instrument shall meet all performance requirements when operated at ambient temperatures from 15 to 25 °C.*/
        }
        requirement <'SR-003'> spatialResolution {
            doc /* The instrument shall resolve features as small as 1 µm across the full imaging area.*/
        }
        requirement <'SR-004'> focusAccuracy {
            doc /* The instrument shall keep the sample within ±0.5 µm of best focus across the imaging area throughout a run.*/
        }
        requirement <'SR-005'> overTemperatureProtection {
        doc /* If the stage temperature exceeds 40 °C during a run, the instrument shall stop heating and laser emission within 5 seconds, end the run, and show an over-temperature error to the user. */
        }   
        requirement <'CR-001'> objectiveNA {
            doc /* The objective shall have a numerical aperture of at least 0.4. */
        }
        requirement <'CR-002'> pixelSizeAtSample {
            doc /* The optics and camera shall provide a pixel size at the sample of 0.5 um or less. */
        }
        requirement <'CR-003'> cameraFormat {
            doc /* The camera shall have at least 1920 x 1080 pixels. */
        }
        requirement <'CR-004'> focusStepSize {
            doc /* The focus drive shall move in steps of 0.1 um or smaller. */
        }
        requirement <'CR-005'> stageTemperatureStability {
            doc /* The thermal controller shall hold the stage within +/-0.5 C of setpoint at ambient temperatures from 15 to 25 C. */
        }
        requirement <'CR-006'> stageTilt {
            doc /* After alignment, the sample plane shall be perpendicular to the optical axis within 0.5 mrad.*/
        }
        requirement <'CR-007'> stageFlatness {
            doc /* The stage surface shall be flat within 0.3 µm peak-to-valley across the imaging area, after tilt removal.*/
        }
    }
    package Structure {
        private import ScalarValues::*;
        part def ImagingInstrument {
            exhibit state instrumentStates : Behavior::InstrumentStates;
            part deck : SampleDeck;
            part fluidics : FluidicsModule;
            part stage : MotionStage;
            part thermal : ThermalController;
            part electronics : ControlElectronics;
            part software : ControlSoftware;
            connection connect optics.imageOut to electronics.imageIn;
            connection connect electronics.motionOut to stage.motionIn;
            connection connect software.controlLink to electronics.hostLink;
            connection connect fluidics.reagentOut to deck.reagentIn;
            connection connect electronics.thermalLink to thermal.controlLink;
            connection connect thermal.heatOut to stage.heatIn;
            part optics : OpticalModule;
        }
        part imagingInstrument : ImagingInstrument;
        part def SampleDeck {
            port reagentIn : ~FluidPort;
        }
        part def FluidicsModule {
            port reagentOut : FluidPort;
        }
        part def MotionStage {
            port motionIn : MotionPort;
            port heatIn : ~HeatPort;
        }
        part def OpticalModule {
            part laser : Laser;
            part 'objective' : Objective;
            part camera : Camera;
            part focus : FocusDrive;
            port imageOut : ImageDataPort;
        }
        part def ThermalController {
            port controlLink : ThermalControlPort;
            port heatOut : HeatPort;
            attribute setpointStability_C : Real = 0.3;
        }
        part def ControlElectronics {
            port imageIn : ~ImageDataPort;
            port motionOut : ~MotionPort;
            port hostLink : ControlPort;
            port thermalLink : ~ThermalControlPort;
        }
        part def ControlSoftware {
            port controlLink : ~ControlPort;
        }
        item def ImageData;
        item def MotionCommand;
        item def RunCommand;
        item def InstrumentStatus;
        item def Reagent;
        item def TemperatureSetpoint;
        item def TemperatureReading;
        item def Heat;
        port def ThermalControlPort {
            in item setpoint : TemperatureSetpoint;
            out item reading : TemperatureReading;
        }
        port def HeatPort {
            out item heat : Heat;
        }
        port def ImageDataPort {
            out item image : ImageData;
        }
        port def MotionPort {
            in item command : MotionCommand;
        }
        port def ControlPort {
            in item command : RunCommand;
            out item status : InstrumentStatus;
        }
        port def FluidPort {
            out item reagent : Reagent;
        }
        view 'Instrument Structure' : DS_Views::SymbolicViews::gv;
        part def Laser;
        part def Objective {
            attribute numericalAperture : Real = 0.45;
            attribute magnification : Real = 20.0;
        }
        part def Camera {
            attribute pixelsHorizontal : Integer = 1920;
            attribute pixelsVertical : Integer = 1080;
            attribute pixelSize_um : Real = 6.5;
        }
        part def FocusDrive {
            attribute stepSize_um : Real = 0.05;
        }
    }
    package Behavior {
        private import ScalarValues::*;
        action def RunWorkflow {
            doc /* One full run, from sample loading to results. SR-001 allows 120 minutes. */
            first start;
            then action loadSample {
                attribute duration_min : Real = 5;
            }
            then fork prepFork;
            then primeFluidics;
            then heatToSetpoint;
            action primeFluidics {
                attribute duration_min : Real = 10;
            }
            then prepJoin;
            action heatToSetpoint {
                attribute duration_min : Real = 10;
            }
            then prepJoin;
            join prepJoin;
            then action positionSample {
                attribute duration_min : Real = 2;
            }
            then action alignStage : AlignStage {
                attribute duration_min : Real = 3;
            }
            then action autofocus {
                attribute duration_min : Real = 5;
            }
            then action acquireImages {
                attribute duration_min : Real = 60;
            }
            then action processImages {
                attribute duration_min : Real = 20;
            }
            then action reportResults {
                attribute duration_min : Real = 5;
            }
            then done;
            view RunWorkflowView : DS_Views::SymbolicViews::afv;
        }
        action def AlignStage {
            doc /*    Runs in every run, after the stage reaches setpoint: level the sample plane to within 0.5 mrad of the optical axis (CR-006).*/
            first start;
            then action measureFocusGrid;
            then action adjustTilt;
            then action confirmTilt;
            then done;
        }
        action runWorkflow : RunWorkflow;
        attribute def PowerOn;
        attribute def PowerOff;
        attribute def SelfTestPassed;
        attribute def SelfTestFailed;
        attribute def StartRun;
        attribute def AbortRun;
        attribute def OverTemperature;
        attribute def Reboot;

        state def InstrumentStates {
            doc /* Operating modes of the instrument, from power-on to power-off. */
            entry ; then off;

            state off;
            transition off_to_on first off accept PowerOn then on;

            state on {
                entry ; then initializing;

                state initializing;
                transition init_to_ready first initializing accept SelfTestPassed then ready;
                transition init_to_error first initializing accept SelfTestFailed then error;

                state ready;
                transition ready_to_running first ready accept StartRun then running;

                state running {
                    do runWorkflow;
                }
                transition running_to_ready first running then ready;
                transition running_to_aborting first running accept AbortRun then aborting;
                transition running_to_error first running accept OverTemperature then error;

                state aborting {
                    doc /* Stop safely: laser off, stage parked, fluidics stopped. */
                    do action safeStop;
                }
                transition aborting_to_ready first aborting then ready;

                state error;
                transition error_to_initializing first error accept Reboot then initializing;
            }
            transition on_to_off first on accept PowerOff then off;
}
    }
    package Verification {
        private import Requirements::*;
        private import Structure::*;
        verification def <'VT-01'> RunTimeTest {
            doc 
            /* Method: Test. Run a full sample load; time it from run start to results available.
             * Pass: 2 hours or less. */
            subject instrument : ImagingInstrument;
            objective {
                verify runTime;
            }
        }
        verification def <'VT-02'> TemperatureChamberTest {
            doc 
            /* Method: Test. Operate in an environmental chamber at 15 C and 25 C ambient; log stage temperature.
             * Pass: all performance specs met; stage within +/-0.5 C of setpoint. */
            subject instrument : ImagingInstrument;
            objective {
                verify operatingTemperature;
                verify stageTemperatureStability;
            }
        }
        verification def <'VT-03'> ResolutionTargetTest {
            doc 
            /* Method: Test. Image a certified resolution target across the full imaging area.
             * Pass: 1 um features resolved everywhere. */
            subject instrument : ImagingInstrument;
            objective {
                verify spatialResolution;
            }
        }
        verification def <'VT-04'> FocusStabilityTest {
            doc 
            /* Method: Test. Measure focus against best focus (z-stack) across the imaging area during a full run.
             * Pass: within +/-0.5 um throughout. */
            subject instrument : ImagingInstrument;
            objective {
                verify focusAccuracy;
            }
        }
        verification def <'VA-01'> OpticalDesignAnalysis {
            doc 
            /* Method: Analysis. Check NA against the Rayleigh limit; pixel size at sample = camera pixel / magnification.
             * Pass: NA >= 0.4 and pixel size at sample <= 0.5 um. */
            subject optics : OpticalModule;
            objective {
                verify objectiveNA;
                verify pixelSizeAtSample;
            }
        }
        verification def <'VI-01'> ComponentSpecInspection {
            doc 
            /* Method: Inspection. Review supplier datasheets and incoming inspection records.
             * Pass: camera >= 1920 x 1080 pixels; focus drive step <= 0.1 um. */
            subject instrument : ImagingInstrument;
            objective {
                verify cameraFormat;
                verify focusStepSize;
            }
        }
        verification def <'VT-05'> StageAngle {
            doc 
            /* Method: Test. Measure best-focus height on a grid across the imaging area and fit a plane.
            * Pass: tilt within 0.5 mrad; flatness within 0.3 µm peak-to-valley after tilt removal. */
            subject stage : MotionStage;
            objective {
                verify stageTilt;
                verify stageFlatness;
            }
        }
        verification def <'VT-06'> OverTemperatureFaultTest {
            doc /* Method: Test. During a run, simulate a stage temperature above 40 °C. Pass: heating and laser emission stop within 5 s, the run ends, and the over-temperature error is shown. Then reboot and confirm the self-test runs before the instrument returns to ready. */
            subject instrument : ImagingInstrument;
            objective {
                verify overTemperatureProtection;
            }
        }
    }
    package Analysis {
        private import ScalarValues::*;
        private import RealFunctions::*;
        private import Structure::*;
        calc def RSS4 {
            in a : Real;
            in b : Real;
            in c : Real;
            in d : Real;
            return : Real = sqrt(a * a + b * b + c * c + d * d);
        }
        part def FocusErrorBudget {
            doc /* Focus error budget for SR-004 (+/-0.5 um). Contributors combined by RSS. */
            attribute allocation_um : Real = 0.5;
            attribute thermalSensitivity_um_per_C : Real = 0.5;
            attribute driveQuantization_um : Real = imagingInstrument.optics.focus.stepSize_um / 2;
            attribute thermalDrift_um : Real = thermalSensitivity_um_per_C * imagingInstrument.thermal.setpointStability_C;
            attribute stageFlatness_um : Real = 0.3;
            attribute autofocusRepeatability_um : Real = 0.2;
            attribute total_um : Real = RSS4(driveQuantization_um, thermalDrift_um, stageFlatness_um, autofocusRepeatability_um);
            attribute margin_um : Real = allocation_um - total_um;
        }
    }
    package Traceability {
        private import Requirements::*;
        private import Structure::*;
        private import RequirementDerivation::*;
        private import Behavior::*;
        satisfy runTime by imagingInstrument;
        satisfy operatingTemperature by imagingInstrument;
        satisfy spatialResolution by imagingInstrument;
        satisfy focusAccuracy by imagingInstrument;
        satisfy objectiveNA by imagingInstrument.optics.'objective';
        satisfy pixelSizeAtSample by imagingInstrument.optics;
        satisfy cameraFormat by imagingInstrument.optics.camera;
        satisfy focusStepSize by imagingInstrument.optics.focus;
        satisfy stageTemperatureStability by imagingInstrument.thermal;
        satisfy overTemperatureProtection by imagingInstrument.instrumentStates;
        #derivation connection {
            end #original ::> spatialResolution;
            end #derive ::> objectiveNA;
            end #derive ::> pixelSizeAtSample;
            end #derive ::> cameraFormat;
        }
        #derivation connection {
            end #original ::> focusAccuracy;
            end #derive ::> focusStepSize;
            end #derive ::> stageTemperatureStability;
            end #derive ::> stageTilt;
            end #derive ::> stageFlatness;
        }
        #derivation connection {
            end #original ::> operatingTemperature;
            end #derive ::> stageTemperatureStability;
        }
        view 'Requirement Traceability' : DS_Views::SymbolicViews::gv;
        satisfy stageTilt by imagingInstrument.stage;
        satisfy stageFlatness by imagingInstrument.stage;
        allocation allocate runWorkflow.loadSample to imagingInstrument.deck;
        allocation allocate runWorkflow.primeFluidics to imagingInstrument.fluidics;
        allocation allocate runWorkflow.heatToSetpoint to imagingInstrument.thermal;
        allocation allocate runWorkflow.positionSample to imagingInstrument.stage;
        allocation allocate runWorkflow.alignStage.measureFocusGrid to imagingInstrument.optics;
        allocation allocate runWorkflow.alignStage.adjustTilt to imagingInstrument.stage;
        allocation allocate runWorkflow.autofocus to imagingInstrument.optics.focus;
        allocation allocate runWorkflow.acquireImages to imagingInstrument.optics.camera;
        allocation allocate runWorkflow.processImages to imagingInstrument.electronics;
        allocation allocate runWorkflow.reportResults to imagingInstrument.software;
        allocate runWorkflow.alignStage.confirmTilt to imagingInstrument.optics;
        allocate runWorkflow.acquireImages to imagingInstrument.optics.laser;
        allocate imagingInstrument.instrumentStates.on.aborting.safeStop to imagingInstrument.electronics;
    }
}
